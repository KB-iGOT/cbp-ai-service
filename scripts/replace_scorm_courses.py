"""
Remove retired courses (by content_id) from the CBP database.

Every content_id in the input CSV is removed -- nothing is replaced or added -- from:

    1. course_metadata_weightage   matching rows are DELETED, so the course can no longer be
                                   recommended. Done first, so a recommendation run during the
                                   cleanup cannot bring it back.
    2. recommended_courses         filtered_courses[] entries (actual_courses is left as is)
    3. suggested_courses           course_identifiers[] (array of bare id strings)
    4. cbp_plans                   selected_courses[] entries
    5. approval_request_items      cbp_plan_data[].selected_courses[] entries -- ONLY with
                                   --include-approval-items; all statuses unless narrowed with
                                   --approval-statuses (e.g. pending,approved)

Only the matching entries are taken out of each JSON array; everything else and the order stay.

Performance and DB load (sized for 60k+ affected rows and thousands of content_ids):
  - Course identifiers live inside JSON arrays, so no index can find them: each table is scanned
    ONCE for all content_ids together (returning only row ids), and every matching row is
    rewritten once.
  - The content_ids are put into the scan SQL as a constant array, never as a bind parameter.
    Postgres (14+) hashes a constant array, so each JSON entry costs one hash lookup; a parameter
    can end up in a generic plan that compares every entry against every id -- measured on 60k
    rows x 40 entries x 6k ids: 2.5s vs 425s. Only validated do_ ids (letters, digits, '_') are inlined, so
    the SQL cannot be injected into.
  - Rows are re-read, rewritten and updated in batches of --batch-size, each batch on --execute in
    its own short transaction locking only its own rows (SELECT ... FOR UPDATE), with --pause
    seconds between batches to spread WAL / replication / autovacuum load.
  - lock_timeout is set for the session: if the app holds a lock on a row, the batch fails fast
    instead of queueing (and making app queries queue behind it).
  - If a batch fails the run stops; committed batches stay committed and a re-run is safe
    (removed content_ids are simply no longer found).

Without --execute the run is a dry run: same scans and counts, no DELETE or UPDATE.

Input:  CSV with a 'content_id' column (other columns ignored). Invalid (non do_) and repeated
        content_ids are skipped and reported.
Output: outcome CSV next to the input (or --out) -- one row per content_id with its status and
        per-table counts; log under scripts/logs/ -- progress, summary and one line per changed DB
        row listing the content_ids removed from it (that detail goes to the log file only).

Usage:
    python scripts/replace_scorm_courses.py --csv retired.csv                     # dry run
    python scripts/replace_scorm_courses.py --csv retired.csv --execute
    python scripts/replace_scorm_courses.py --csv retired.csv --include-approval-items --execute
    python scripts/replace_scorm_courses.py --csv retired.csv --include-approval-items \
        --approval-statuses pending,approved

Requires DATABASE_URL (environment or .env in the cwd / repo root), e.g.
postgresql://user:pass@host:5432/dbname ("postgresql+asyncpg://" is accepted).
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import asyncpg

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_NAME = Path(__file__).stem
LOGS_DIR = SCRIPT_DIR / "logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

logger = logging.getLogger(SCRIPT_NAME)

METADATA_TABLE = "course_metadata_weightage"
APPROVAL_TABLE = "approval_request_items"
TABLES = [METADATA_TABLE, "recommended_courses", "suggested_courses", "cbp_plans", APPROVAL_TABLE]

APPROVAL_STATUSES = {"draft", "pending", "approved", "rejected", "failed"}

DEFAULT_BATCH_SIZE = 500
DEFAULT_PAUSE_SECONDS = 0.1
LOCK_TIMEOUT = "5s"
TARGET_IDS = "__TARGET_IDS__"  # replaced in scan SQL by the constant id array
CONTENT_ID_RE = re.compile(r"^do_\w+$")


def _load_dotenv() -> None:
    """Fill os.environ from .env (cwd, then repo root); real environment variables win."""
    for env_path in (Path(".env"), SCRIPT_DIR.parent / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _database_dsn() -> str:
    _load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("Missing required environment variable: DATABASE_URL")
    # asyncpg needs plain postgresql://; the app's .env uses the SQLAlchemy dialect form.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@dataclass
class TableResult:
    rows: int = 0      # DB rows containing the course (rewritten, or deleted for the metadata table)
    removed: int = 0   # entries removed (metadata table: rows deleted)


# {content_id: entries removed} for the DB row currently being rewritten.
RowHits = Dict[str, int]


# --- Removing entries from JSON arrays -----------------------------------------------------

def remove_from_list(values, targets: Set[str], hits: RowHits,
                     bare_ids: bool = False) -> Tuple[Any, bool]:
    """Drop the entries whose identifier is in `targets`, order kept. `values` holds course
    objects, or bare id strings when `bare_ids`. A non-list value is returned untouched."""
    if not isinstance(values, list):
        return values, False
    result = []
    for value in values:
        if bare_ids:
            content_id = str(value).strip() if value is not None else None
        else:
            content_id = (str(value.get("identifier")).strip()
                          if isinstance(value, dict) and value.get("identifier") is not None
                          else None)
        if content_id in targets:
            hits[content_id] += 1
        else:
            result.append(value)
    return result, len(result) != len(values)


def remove_from_plan_data(data, targets: Set[str], hits: RowHits) -> Tuple[Any, bool]:
    """cbp_plan_data is a list of plan snapshots (a single object is tolerated), each with its
    own selected_courses list."""
    plans = data if isinstance(data, list) else [data]
    new_plans, changed = [], False
    for plan in plans:
        if isinstance(plan, dict):
            courses, plan_changed = remove_from_list(plan.get("selected_courses"), targets, hits)
            if plan_changed:
                plan = {**plan, "selected_courses": courses}
                changed = True
        new_plans.append(plan)
    return (new_plans if isinstance(data, list) else new_plans[0]), changed


# --- JSON-array tables ---------------------------------------------------------------------
#
# scan_sql   one pass over the table for ALL content_ids ($1 text[]) -> matching row ids
# fetch_sql  full rows for one batch of ids ($1 uuid[]); FOR UPDATE is appended on --execute
# rewrite    row -> new column values in update_sql's $2.. order, or None if nothing to remove
# update_sql one row's UPDATE, sent with executemany for the whole batch

def _as_array(expr: str) -> str:
    """Anything that is not a JSON array reads as [], so jsonb_array_elements() never errors."""
    return f"CASE WHEN jsonb_typeof({expr}) = 'array' THEN {expr} ELSE '[]'::jsonb END"


def _has_course(col: str) -> str:
    return (f"EXISTS (SELECT 1 FROM jsonb_array_elements({_as_array(col)}) e "
            f"WHERE e->>'identifier' = ANY({TARGET_IDS}))")


def _ids_literal(targets: Set[str]) -> str:
    """The content_ids as a constant text[] for the SQL text (see module docstring for why).
    Re-checked here so nothing but a validated do_ id can ever reach the SQL."""
    bad = [t for t in targets if not CONTENT_ID_RE.match(t)]
    if bad:
        raise ValueError(f"refusing to inline invalid content_ids: {bad[:5]}")
    return "ARRAY[" + ",".join(f"'{t}'" for t in sorted(targets)) + "]::text[]"


def _with_ids(sql: str, targets: Set[str]) -> str:
    return sql.replace(TARGET_IDS, _ids_literal(targets))


@dataclass
class TableSpec:
    table: str
    column: str
    scan_sql: str
    fetch_sql: str
    update_sql: str
    rewrite: Callable[[Any, Set[str], RowHits], Optional[tuple]]
    uses_statuses: bool = False  # scan takes the approval status filter as $1, fetch as $2


def _rewrite_column(column: str, bare_ids: bool = False):
    def rewrite(row, targets, hits):
        values, changed = remove_from_list(row[column], targets, hits, bare_ids)
        return (values,) if changed else None
    return rewrite


def _rewrite_approval_item(row, targets, hits):
    data, changed = remove_from_plan_data(row["data"], targets, hits)
    return (data,) if changed else None


def _status_filter(param: str) -> str:
    """Approval status filter on bind parameter `param`; NULL = every status."""
    return f"({param}::text[] IS NULL OR lower(a.status::text) = ANY({param}::text[]))"


TABLE_SPECS = [
    TableSpec(
        table="recommended_courses",
        column="filtered_courses",
        scan_sql=f"SELECT id FROM recommended_courses WHERE {_has_course('filtered_courses')}",
        fetch_sql="SELECT id, filtered_courses FROM recommended_courses WHERE id = ANY($1::uuid[])",
        update_sql="""UPDATE recommended_courses
                         SET filtered_courses = $2::jsonb, updated_at = now() WHERE id = $1""",
        rewrite=_rewrite_column("filtered_courses"),
    ),
    TableSpec(
        table="suggested_courses",
        column="course_identifiers",
        scan_sql=f"""SELECT id FROM suggested_courses
                      WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(
                                        {_as_array('course_identifiers')}) v
                                     WHERE v = ANY({TARGET_IDS}))""",
        fetch_sql="SELECT id, course_identifiers FROM suggested_courses WHERE id = ANY($1::uuid[])",
        update_sql="""UPDATE suggested_courses
                         SET course_identifiers = $2::jsonb, updated_at = now() WHERE id = $1""",
        rewrite=_rewrite_column("course_identifiers", bare_ids=True),
    ),
    TableSpec(
        table="cbp_plans",
        column="selected_courses",
        scan_sql=f"SELECT id FROM cbp_plans WHERE {_has_course('selected_courses')}",
        fetch_sql="SELECT id, selected_courses FROM cbp_plans WHERE id = ANY($1::uuid[])",
        update_sql="""UPDATE cbp_plans
                         SET selected_courses = $2::jsonb, updated_at = now() WHERE id = $1""",
        rewrite=_rewrite_column("selected_courses"),
    ),
    TableSpec(
        table=APPROVAL_TABLE,
        column="cbp_plan_data",
        # cbp_plan_data is JSON (not JSONB); a single snapshot object counts as a one-item list.
        scan_sql=f"""SELECT a.id
                       FROM approval_request_items a
                      CROSS JOIN LATERAL (SELECT a.cbp_plan_data::jsonb AS d) x
                      WHERE {_status_filter('$1')}
                        AND EXISTS (
                            SELECT 1
                              FROM jsonb_array_elements(CASE jsonb_typeof(x.d)
                                       WHEN 'array'  THEN x.d
                                       WHEN 'object' THEN jsonb_build_array(x.d)
                                       ELSE '[]'::jsonb END) plan,
                                   jsonb_array_elements({_as_array("plan->'selected_courses'")}) c
                             WHERE c->>'identifier' = ANY({TARGET_IDS}))""",
        fetch_sql=f"""SELECT a.id, a.cbp_plan_data::jsonb AS data
                        FROM approval_request_items a
                       WHERE a.id = ANY($1::uuid[]) AND {_status_filter('$2')}""",
        # No updated_at column on this table; the value is written back as json.
        update_sql="UPDATE approval_request_items SET cbp_plan_data = $2::json WHERE id = $1",
        rewrite=_rewrite_approval_item,
        uses_statuses=True,
    ),
]


async def process_table(conn, spec: TableSpec, targets: Set[str],
                        stats: Dict[str, Dict[str, TableResult]],
                        totals: Dict[str, TableResult], execute: bool, batch_size: int,
                        pause: float, statuses: Optional[List[str]]) -> None:
    """Scan the table once, then rewrite and update the matching rows batch by batch."""
    extra = (statuses,) if spec.uses_statuses else ()
    started = time.monotonic()
    ids = [r["id"] for r in await conn.fetch(_with_ids(spec.scan_sql, targets), *extra)]
    logger.info(f"{spec.table}: {len(ids)} matching rows (scan {time.monotonic() - started:.1f}s)")

    fetch_sql = spec.fetch_sql + (" FOR UPDATE" if execute else "")
    total = totals[spec.table]

    async def apply_batch(batch):
        updates = []
        for row in await conn.fetch(fetch_sql, batch, *extra):
            hits: RowHits = defaultdict(int)
            values = spec.rewrite(row, targets, hits)
            if values is None:
                continue  # changed since the scan; nothing left to remove
            updates.append((row["id"], *values))
            logger.debug(f"{spec.table} id={row['id']} {spec.column}: "
                         f"{'removed' if execute else 'would remove'} "
                         + ", ".join(f"{cid} x{n}" if n > 1 else cid for cid, n in hits.items()))
            for content_id, removed in hits.items():
                stats[content_id][spec.table].rows += 1
                stats[content_id][spec.table].removed += removed
                total.removed += removed
        if execute and updates:
            await conn.executemany(spec.update_sql, updates)
        total.rows += len(updates)

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        batch_started = time.monotonic()
        try:
            if execute:
                async with conn.transaction():
                    await apply_batch(batch)
            else:
                await apply_batch(batch)
        except Exception:
            logger.error(f"{spec.table}: batch {start + 1}-{start + len(batch)} failed and was "
                         f"rolled back; earlier batches are committed, re-running is safe.")
            raise
        logger.info(f"{spec.table}: {start + len(batch)}/{len(ids)} rows processed "
                    f"({time.monotonic() - batch_started:.1f}s)")
        if pause and start + batch_size < len(ids):
            await asyncio.sleep(pause)


async def delete_from_course_metadata(conn, targets: Set[str],
                                      stats: Dict[str, Dict[str, TableResult]],
                                      totals: Dict[str, TableResult], execute: bool) -> None:
    """Delete the courses' metadata rows in one statement (one transaction under --execute).
    Only identifier and name are read back -- the embedding columns are large."""
    started = time.monotonic()
    # Uses the btree index on identifier.
    where = f"FROM public.{METADATA_TABLE} WHERE identifier = ANY({_ids_literal(targets)})"
    if execute:
        async with conn.transaction():
            rows = await conn.fetch(f"DELETE {where} RETURNING identifier, name")
    else:
        rows = await conn.fetch(f"SELECT identifier, name {where}")

    for row in rows:
        logger.debug(f"{METADATA_TABLE}: {'deleted' if execute else 'would delete'} "
                     f"{row['identifier']} ({row['name']})")
        stats[row["identifier"]][METADATA_TABLE].rows += 1
        stats[row["identifier"]][METADATA_TABLE].removed += 1
    totals[METADATA_TABLE].rows = totals[METADATA_TABLE].removed = len(rows)
    logger.info(f"{METADATA_TABLE}: {len(rows)} rows {'deleted' if execute else 'matching'} "
                f"({time.monotonic() - started:.1f}s)")


# --- Input / output ------------------------------------------------------------------------

def read_input(path: Path) -> Tuple[List[Tuple[int, str]], Dict[int, Tuple[str, str]]]:
    """Returns ([(line_no, content_id)], {line_no: (skip_status, reason)})."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = [re.sub(r"[^a-z0-9]", "", h.lower()) for h in next(reader, [])]
        if "contentid" not in header:
            raise SystemExit(f"{path}: expected a 'content_id' column")
        column = header.index("contentid")
        rows = [(line_no, values[column].strip())
                for line_no, values in enumerate(reader, start=2)
                if column < len(values) and values[column].strip()]

    skipped: Dict[int, Tuple[str, str]] = {}
    first_seen: Dict[str, int] = {}
    for line_no, content_id in rows:
        if not CONTENT_ID_RE.match(content_id):
            skipped[line_no] = ("skipped_invalid_content_id", repr(content_id))
        elif content_id in first_seen:
            skipped[line_no] = ("skipped_duplicate_row", f"same as line {first_seen[content_id]}")
        else:
            first_seen[content_id] = line_no
    return rows, skipped


def write_report(path: Path, rows, skipped, stats, tables: List[str], mode: str) -> Counter:
    """One row per input row, in file order. Returns the status counts."""
    header = ["line_no", "content_id", "mode", "status", "reason"]
    for t in tables:
        header += ([f"{t}_rows_deleted"] if t == METADATA_TABLE
                   else [f"{t}_rows_updated", f"{t}_entries_removed"])
    header.append("total_rows_impacted")

    statuses: Counter = Counter()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for line_no, content_id in rows:
            if line_no in skipped:
                status, reason = skipped[line_no]
                counts = [""] * (len(header) - 5)
            else:
                per_table = stats[content_id]
                status = "removed" if any(per_table[t].rows for t in tables) else "no_match"
                reason, counts = "", []
                for t in tables:
                    r = per_table[t]
                    counts += [r.rows] if t == METADATA_TABLE else [r.rows, r.removed]
                counts.append(sum(per_table[t].rows for t in tables))
            statuses[status] += 1
            writer.writerow([line_no, content_id, mode, status, reason, *counts])
    return statuses


def configure_logging(log_file: Path) -> None:
    """Log file: everything, incl. one line per changed row (DEBUG). Terminal: INFO only."""
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler, level in ((logging.FileHandler(log_file, encoding="utf-8"), logging.DEBUG),
                           (logging.StreamHandler(sys.stdout), logging.INFO)):
        handler.setLevel(level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


# --- Main ----------------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Remove courses (by content_id) from course_metadata_weightage, "
                    "recommended_courses, suggested_courses, cbp_plans and optionally "
                    "approval_request_items.",
    )
    parser.add_argument("--csv", type=Path, required=True,
                        help="Input CSV with a 'content_id' column")
    parser.add_argument("--execute", action="store_true",
                        help="Commit the changes. Without this the run is a dry run.")
    parser.add_argument("--include-approval-items", action="store_true",
                        help="Also remove the courses from approval_request_items")
    parser.add_argument("--approval-statuses", default=None,
                        help="With --include-approval-items: comma-separated statuses to update, "
                             "e.g. 'pending,approved' (default: all statuses)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Rows per batch / transaction (default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SECONDS,
                        help=f"Seconds to wait between batches to limit DB load "
                             f"(default: {DEFAULT_PAUSE_SECONDS})")
    parser.add_argument("--out", type=Path, default=None,
                        help="Outcome CSV path (default: <input>_<dryrun|execute>_<timestamp>.csv "
                             "next to the input)")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.pause < 0:
        parser.error("--pause cannot be negative")
    if args.approval_statuses:
        if not args.include_approval_items:
            parser.error("--approval-statuses only applies with --include-approval-items")
        statuses = [s.strip().lower() for s in args.approval_statuses.split(",") if s.strip()]
        unknown = set(statuses) - APPROVAL_STATUSES
        if unknown:
            parser.error(f"unknown --approval-statuses {sorted(unknown)}; "
                         f"allowed: {sorted(APPROVAL_STATUSES)}")
        args.approval_statuses = statuses
    return args


async def main() -> None:
    args = _parse_args()
    csv_path = args.csv.expanduser()
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{SCRIPT_NAME}_{mode.lower()}_{RUN_TIMESTAMP}.log"
    out_path = args.out or csv_path.resolve().parent / (
        f"{csv_path.stem}_{mode.lower().replace('-', '')}_{RUN_TIMESTAMP}.csv"
    )
    configure_logging(log_file)

    rows, skipped = read_input(csv_path)
    if not rows:
        raise SystemExit(f"No content_ids found in {csv_path}.")
    targets = {cid for line_no, cid in rows if line_no not in skipped}
    tables = [t for t in TABLES if t != APPROVAL_TABLE or args.include_approval_items]

    logger.info(f"Mode: {mode}  |  content_ids: {len(targets)} to remove, "
                f"{len(skipped)} skipped (invalid/duplicate)")
    logger.info(f"Tables: {', '.join(tables)}")
    if args.include_approval_items:
        logger.info("approval_request_items statuses: "
                    + (", ".join(args.approval_statuses) if args.approval_statuses else "all"))

    stats: Dict[str, Dict[str, TableResult]] = defaultdict(
        lambda: {t: TableResult() for t in TABLES}
    )
    totals = {t: TableResult() for t in TABLES}
    run_started = time.monotonic()

    if targets:
        conn = await asyncpg.connect(
            _database_dsn(),
            # Named in pg_stat_activity; fail fast rather than queue behind app locks.
            server_settings={"application_name": SCRIPT_NAME, "lock_timeout": LOCK_TIMEOUT},
        )
        try:
            # Exchange JSON as Python objects; keep non-ASCII (e.g. Hindi course names) as-is
            # rather than \u-escaping it, since json columns store the text verbatim.
            for typename in ("json", "jsonb"):
                await conn.set_type_codec(
                    typename, encoder=lambda v: json.dumps(v, ensure_ascii=False),
                    decoder=json.loads, schema="pg_catalog",
                )
            logger.info(f"Batch size: {args.batch_size}  pause: {args.pause}s  "
                        f"lock_timeout: {LOCK_TIMEOUT}  statement_timeout: "
                        f"{await conn.fetchval('SHOW statement_timeout')}")
            await delete_from_course_metadata(conn, targets, stats, totals, args.execute)
            for spec in TABLE_SPECS:
                if spec.table in tables:
                    await process_table(conn, spec, targets, stats, totals,
                                        args.execute, args.batch_size, args.pause,
                                        args.approval_statuses)
        finally:
            await conn.close()

    statuses = write_report(out_path, rows, skipped, stats, tables, mode)

    logger.info(f"Content_id outcomes: {dict(statuses)}")
    logger.info(f"{'Removed' if args.execute else 'Would remove'} (rows / entries):")
    for t in tables:
        logger.info(f"  {t:<26} {totals[t].rows:>7} / {totals[t].removed:>7}")
    logger.info(f"Finished in {time.monotonic() - run_started:.1f}s")
    logger.info(f"Outcome CSV: {out_path}")
    logger.info(f"Log: {log_file}")
    logger.info("Committed." if args.execute
                else "Dry run - nothing was changed. Re-run with --execute to apply.")


if __name__ == "__main__":
    asyncio.run(main())

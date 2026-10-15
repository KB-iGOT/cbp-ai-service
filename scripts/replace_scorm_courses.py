"""
Replace retired SCORM course references with their new non-SCORM course across the CBP database.

The old SCORM content has been deleted from iGOT and a non-SCORM copy published under a new
identifier. Every place the old identifier is stored is pointed at the new one instead, in:

    1. recommended_courses      filtered_courses[].identifier
                                (actual_courses, the raw vector-search audit trail, is left as is)
    2. suggested_courses        course_identifiers[]            (array of bare id strings)
    3. cbp_plans                selected_courses[].identifier
    4. approval_request_items   cbp_plan_data[].selected_courses[].identifier
                                (every status by default; narrow with --approval-statuses)

What is copied: the existing entry is kept as-is (relevancy, rationale, is_public, distance, ...)
and only its identifier is swapped, so the new course takes over exactly the position and data the
old one had. The course-specific fields the entry already carries -- course/name, competencies /
competencies_v6, duration, organisation, description, keywords -- are then refreshed from
course_metadata_weightage for the new identifier. No key is added or removed, so every entry keeps
its original shape.

If the new course is not in course_metadata_weightage (metadata_found=no), --on-missing-metadata
decides what happens:
    remove   (default) the old SCORM entry is removed everywhere and the new course is NOT added
             -- row status 'removed_no_metadata'
    replace  the entry is replaced anyway; only course/name is updated (from the CSV's Non-SCORM
             Course Name), every other field keeps the old SCORM course's values
    skip     the row is not touched at all -- the old SCORM entry stays ('skipped_no_metadata')

If the new identifier is already present in the same list, the old entry is removed rather than
replaced, so no list ever ends up holding the same course twice. Both kinds of removal are reported
per table as entries_removed and logged with their reason. Array order is otherwise preserved.

How it runs (built for production volumes): course identifiers live inside JSON arrays, so no
index can find them and every lookup is a full table scan. Each table is therefore scanned ONCE for
all old identifiers together (returning only row ids), and every matching row is then rewritten
once with all mappings applied in a single pass. Rows are re-read, rewritten and updated in batches
of --batch-size; under --execute each batch is its own transaction and locks its rows
(SELECT ... FOR UPDATE) only for that batch, so a concurrent app edit cannot be clobbered and no
lock is held for long. If a batch fails the run stops; batches already committed stay committed,
and a re-run is safe -- identifiers already replaced are simply no longer found.

Nothing is written unless --execute is passed. A plain run is a dry run: it finds and rewrites
exactly the same entries in memory and reports what would change, but issues no UPDATE.

Input CSV columns (header match ignores case, spaces and punctuation):
    SCORM_ID                (required)  old identifier
    Non-SCORM ID            (required)  new identifier
    SCORM Course Name       (optional)  report only
    Non-SCORM Course Name   (optional)  fallback name when the new course has no metadata
    S. No.                  (optional)  report only

Rows are skipped (and reported) when: the Non-SCORM ID is not a do_ identifier (e.g. '-' for
'Upload Pending'), both ids are the same, the SCORM_ID repeats an earlier row, or the new id is
itself the SCORM_ID of another row. Fully blank lines are ignored.

Output:
    - outcome CSV next to the input file (or --out): one row per input row with its status and,
      per table, rows updated / entries replaced / entries removed
    - log under scripts/logs/: progress and summary, plus the OLD -> NEW object of every changed
      entry by default. That is ~1-3 KB per entry, i.e. gigabytes at production volume -- check
      free disk first, or cap it with --log-entries N (first N changes per mapping per table;
      0 = none). Entry-level detail goes to the log file only, not the terminal.
    - terminal: progress and a per-table summary of rows impacted

Usage:
    python scripts/replace_scorm_courses.py --csv "~/Downloads/SCORM to Non_SCORM.csv"
    python scripts/replace_scorm_courses.py --csv "~/Downloads/SCORM to Non_SCORM.csv" --execute
    python scripts/replace_scorm_courses.py --csv mapping.csv --approval-statuses pending,approved

Configuration is read from the environment; a .env file in the current directory or the repo
root is loaded automatically (real environment variables take precedence over .env values).

Required environment variables:
    DATABASE_URL    Postgres connection string
                      e.g. postgresql://user:pass@host:5432/dbname
                      (a "postgresql+asyncpg://" prefix is accepted)
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
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import asyncpg


def _load_dotenv():
    """Populate os.environ from a .env file (cwd first, then this file's repo root) so the settings
    below can be read without exporting them by hand. Real environment variables win (never
    overwritten). Uses python-dotenv if installed, otherwise a minimal hand-parser."""
    try:
        from dotenv import load_dotenv

        load_dotenv()  # searches cwd upward
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass
    for env_path in (Path(".env"), Path(__file__).resolve().parents[1] / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = val.strip().strip('"').strip("'")


_load_dotenv()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _asyncpg_dsn(url: str) -> str:
    """asyncpg needs a plain postgresql:// DSN; .env carries the SQLAlchemy
    'postgresql+asyncpg://' dialect form used by the app."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

logger = logging.getLogger("replace_scorm_courses")

# Tables in processing order.
TABLES = [
    "recommended_courses",
    "suggested_courses",
    "cbp_plans",
    "approval_request_items",
]

APPROVAL_STATUSES = {"draft", "pending", "approved", "rejected", "failed"}

DEFAULT_BATCH_SIZE = 500
DEFAULT_LOG_ENTRIES = -1  # -1 = log every change

# A real content identifier; '-' / 'Upload Pending' / blanks fail this and the row is skipped.
CONTENT_ID_RE = re.compile(r"^do_\w+$")

REASON_DUPLICATE = "new course already in list"
REASON_NO_METADATA = "new course not in course_metadata_weightage"

COURSE_METADATA_SQL = """
    SELECT DISTINCT ON (identifier)
           identifier, name, competencies_v6, duration, organisation, description, keywords
      FROM public.course_metadata_weightage
     WHERE identifier = ANY($1::text[])
"""


# --- Data types ----------------------------------------------------------------------------

@dataclass
class Mapping:
    line_no: int
    s_no: str
    old_id: str
    old_name: str
    new_id: str
    new_name: str


@dataclass
class Action:
    """What to do with one old identifier wherever it is found."""
    m: Mapping
    meta: Optional[dict]
    remove_only: bool  # new course has no metadata and --on-missing-metadata=remove


@dataclass
class TableResult:
    rows: int = 0         # DB rows containing the course (= rows rewritten)
    replaced: int = 0     # entries whose identifier was swapped to the new course
    removed: int = 0      # old entries removed without a replacement (duplicate / no metadata)


# {old_id: [replaced, removed]} for the row currently being rewritten.
RowHits = Dict[str, List[int]]


# --- JSON helpers --------------------------------------------------------------------------

def _json_default(value):
    """course_metadata_weightage hands back numeric/date columns as Decimal/datetime; store them
    the way the app's JSON would have them."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Exchange json/jsonb as Python objects instead of strings."""
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename, encoder=_dumps, decoder=json.loads, schema="pg_catalog"
        )


# --- Course entry rewriting ----------------------------------------------------------------

def _identifier_of(course) -> Optional[str]:
    if not isinstance(course, dict):
        return None
    value = course.get("identifier")
    return str(value).strip() if value is not None else None


def _rebuild_entry(entry: dict, m: Mapping, meta: Optional[dict]) -> dict:
    """Copy of `entry` pointed at the new course. Only keys the entry already has are refreshed,
    and never with a NULL, so the stored shape and every non-course field are preserved."""
    new_entry = dict(entry)
    new_entry["identifier"] = m.new_id

    name = (meta or {}).get("name") or m.new_name
    refreshed = {"course": name, "name": name}
    if meta:
        refreshed.update({
            "competencies": meta.get("competencies_v6"),
            "competencies_v6": meta.get("competencies_v6"),
            "duration": meta.get("duration"),
            "description": meta.get("description"),
            "keywords": meta.get("keywords"),
        })
        org = meta.get("organisation")
        if org is not None and "organisation" in entry:
            # Recommendation entries store organisation as a joined string, search-API entries
            # as a list -- keep whichever form this entry already used.
            if isinstance(entry["organisation"], list):
                refreshed["organisation"] = org if isinstance(org, list) else [org]
            else:
                refreshed["organisation"] = (
                    ", ".join(str(o) for o in org if o) if isinstance(org, list) else org
                )

    for key, value in refreshed.items():
        if key in new_entry and value is not None:
            new_entry[key] = value
    return new_entry


# Per-mapping-per-table cap on OLD/NEW samples in the log (-1 = unlimited); set from
# --log-entries. Counted in _entries_logged keyed by (table, old_id).
_log_entries_limit = DEFAULT_LOG_ENTRIES
_entries_logged: Counter = Counter()


def _log_change(where: str, old_id: str, old, new=None, reason: str = "") -> None:
    """Entry-level detail: log file only (DEBUG), so the terminal is not flooded. With
    --log-entries N only the first N changes per mapping per table are logged."""
    key = (where.split(" ", 1)[0], old_id)  # `where` starts with the table name
    if _log_entries_limit >= 0 and _entries_logged[key] >= _log_entries_limit:
        return
    _entries_logged[key] += 1
    if new is None:
        logger.debug(f"    {where}: REMOVED ({reason}) OLD {_dumps(old)}")
    else:
        logger.debug(f"    {where}:\n      OLD {_dumps(old)}\n      NEW {_dumps(new)}")


def rewrite_list(values, actions: Dict[str, Action], where: str, hits: RowHits,
                 bare_ids: bool = False) -> Tuple[Any, bool]:
    """Apply every mapping to one JSON array in a single pass.

    `values` is a list of course objects, or of bare identifier strings when `bare_ids`.
    Returns (new_list, changed); a non-list value is passed through untouched. Per-old-id
    replaced/removed counts are added to `hits`."""
    if not isinstance(values, list):
        return values, False

    def ident(value):
        return (str(value).strip() if value is not None else None) if bare_ids \
            else _identifier_of(value)

    present = {i for i in map(ident, values) if i is not None}
    result, changed = [], False
    for value in values:
        old_id = ident(value)
        action = actions.get(old_id) if old_id is not None else None
        if action is None:
            result.append(value)
            continue

        changed = True
        new_id = action.m.new_id
        if action.remove_only or new_id in present:
            reason = REASON_NO_METADATA if action.remove_only else REASON_DUPLICATE
            _log_change(where, old_id, value, reason=reason)
            hits[old_id][1] += 1
        else:
            new_value = new_id if bare_ids else _rebuild_entry(value, action.m, action.meta)
            _log_change(where, old_id, value, new_value)
            result.append(new_value)
            present.add(new_id)  # any further copy now counts as a duplicate
            hits[old_id][0] += 1
    return result, changed


def rewrite_plan_data(data, actions: Dict[str, Action], where: str,
                      hits: RowHits) -> Tuple[Any, bool]:
    """cbp_plan_data is a list of CBP plan snapshots (a single snapshot object is tolerated, as
    the publish script does), each with its own selected_courses list."""
    plans = data if isinstance(data, list) else [data]
    new_plans, changed = [], False
    for index, plan in enumerate(plans):
        if isinstance(plan, dict) and isinstance(plan.get("selected_courses"), list):
            courses, plan_changed = rewrite_list(
                plan["selected_courses"], actions, f"{where}[{index}].selected_courses", hits
            )
            if plan_changed:
                plan = {**plan, "selected_courses": courses}
                changed = True
        new_plans.append(plan)
    return (new_plans if isinstance(data, list) else new_plans[0]), changed


# --- Per-table definitions -----------------------------------------------------------------
#
# scan_sql   one full pass over the table for ALL old ids ($1 text[]), returning only row ids
# fetch_sql  the full rows for one batch of ids ($1 uuid[]); FOR UPDATE is appended on --execute
# rewrite    row -> tuple of new column values (in update_sql's $2.. order), or None if unchanged
# update_sql one row's UPDATE, run with executemany for the whole batch

def _as_array(expr: str) -> str:
    """Expand-safe view of a JSON value: anything that is not an array reads as an empty array,
    so jsonb_array_elements() never errors on a NULL or a stray object."""
    return f"CASE WHEN jsonb_typeof({expr}) = 'array' THEN {expr} ELSE '[]'::jsonb END"


def _has_course(col: str) -> str:
    return (f"EXISTS (SELECT 1 FROM jsonb_array_elements({_as_array(col)}) e "
            f"WHERE e->>'identifier' = ANY($1::text[]))")


@dataclass
class TableSpec:
    table: str
    scan_sql: str
    fetch_sql: str
    update_sql: str
    rewrite: Callable[[Any, Dict[str, Action], RowHits], Optional[tuple]]
    uses_statuses: bool = False


def _rewrite_recommended(row, actions, hits):
    filtered, changed = rewrite_list(
        row["filtered_courses"], actions,
        f"recommended_courses id={row['id']} filtered_courses", hits,
    )
    return (filtered,) if changed else None


def _rewrite_suggested(row, actions, hits):
    ids, changed = rewrite_list(
        row["course_identifiers"], actions,
        f"suggested_courses id={row['id']} course_identifiers", hits, bare_ids=True,
    )
    return (ids,) if changed else None


def _rewrite_cbp_plan(row, actions, hits):
    courses, changed = rewrite_list(
        row["selected_courses"], actions, f"cbp_plans id={row['id']} selected_courses", hits
    )
    return (courses,) if changed else None


def _rewrite_approval_item(row, actions, hits):
    data, changed = rewrite_plan_data(
        row["data"], actions, f"approval_request_items id={row['id']} cbp_plan_data", hits
    )
    return (data,) if changed else None


# cbp_plan_data is JSON (not JSONB); a single snapshot object is treated as a one-element list.
_APPROVAL_PLANS = """CASE jsonb_typeof(x.d)
                         WHEN 'array'  THEN x.d
                         WHEN 'object' THEN jsonb_build_array(x.d)
                         ELSE '[]'::jsonb END"""
_STATUS_FILTER = "($2::text[] IS NULL OR lower(a.status::text) = ANY($2::text[]))"

TABLE_SPECS = [
    TableSpec(
        table="recommended_courses",
        scan_sql=f"""SELECT r.id FROM recommended_courses r
                      WHERE {_has_course('r.filtered_courses')}""",
        fetch_sql="""SELECT id, filtered_courses
                       FROM recommended_courses WHERE id = ANY($1::uuid[])""",
        update_sql="""UPDATE recommended_courses
                         SET filtered_courses = $2::jsonb, updated_at = now()
                       WHERE id = $1""",
        rewrite=_rewrite_recommended,
    ),
    TableSpec(
        table="suggested_courses",
        # ?| matches top-level string elements of the array against any of the ids.
        scan_sql="""SELECT s.id FROM suggested_courses s
                     WHERE jsonb_typeof(s.course_identifiers) = 'array'
                       AND s.course_identifiers ?| $1::text[]""",
        fetch_sql="""SELECT id, course_identifiers
                       FROM suggested_courses WHERE id = ANY($1::uuid[])""",
        update_sql="""UPDATE suggested_courses
                         SET course_identifiers = $2::jsonb, updated_at = now()
                       WHERE id = $1""",
        rewrite=_rewrite_suggested,
    ),
    TableSpec(
        table="cbp_plans",
        scan_sql=f"""SELECT p.id FROM cbp_plans p
                      WHERE {_has_course('p.selected_courses')}""",
        fetch_sql="""SELECT id, selected_courses
                       FROM cbp_plans WHERE id = ANY($1::uuid[])""",
        update_sql="""UPDATE cbp_plans
                         SET selected_courses = $2::jsonb, updated_at = now()
                       WHERE id = $1""",
        rewrite=_rewrite_cbp_plan,
    ),
    TableSpec(
        table="approval_request_items",
        scan_sql=f"""SELECT a.id
                       FROM approval_request_items a
                      CROSS JOIN LATERAL (SELECT a.cbp_plan_data::jsonb AS d) x
                      WHERE {_STATUS_FILTER}
                        AND EXISTS (
                            SELECT 1
                              FROM jsonb_array_elements({_APPROVAL_PLANS}) plan,
                                   jsonb_array_elements({_as_array("plan->'selected_courses'")}) c
                             WHERE c->>'identifier' = ANY($1::text[]))""",
        fetch_sql=f"""SELECT a.id, a.cbp_plan_data::jsonb AS data
                        FROM approval_request_items a
                       WHERE a.id = ANY($1::uuid[]) AND {_STATUS_FILTER}""",
        # No updated_at column on this table; the jsonb value is written back as json.
        update_sql="UPDATE approval_request_items SET cbp_plan_data = $2::json WHERE id = $1",
        rewrite=_rewrite_approval_item,
        uses_statuses=True,
    ),
]


async def process_table(
    conn: asyncpg.Connection,
    spec: TableSpec,
    actions: Dict[str, Action],
    stats: Dict[str, Dict[str, TableResult]],
    totals: Dict[str, TableResult],
    execute: bool,
    batch_size: int,
    statuses: Optional[List[str]],
) -> None:
    """Scan the table once, then rewrite and update the matching rows batch by batch."""
    extra = (statuses,) if spec.uses_statuses else ()
    old_ids = list(actions)

    started = time.monotonic()
    ids = [r["id"] for r in await conn.fetch(spec.scan_sql, old_ids, *extra)]
    logger.info(f"{spec.table}: {len(ids)} matching rows (scan {time.monotonic() - started:.1f}s)")

    fetch_sql = spec.fetch_sql + (" FOR UPDATE" if execute else "")
    total = totals[spec.table]

    for start in range(0, len(ids), batch_size):
        batch = ids[start:start + batch_size]
        batch_started = time.monotonic()

        async def apply_batch():
            updates = []
            for row in await conn.fetch(fetch_sql, batch, *extra):
                hits: RowHits = defaultdict(lambda: [0, 0])
                values = spec.rewrite(row, actions, hits)
                if values is None:
                    continue  # changed since the scan; nothing left to do in this row
                updates.append((row["id"], *values))
                for old_id, (replaced, removed) in hits.items():
                    result = stats[old_id][spec.table]
                    result.rows += 1
                    result.replaced += replaced
                    result.removed += removed
                    total.replaced += replaced
                    total.removed += removed
            if execute and updates:
                await conn.executemany(spec.update_sql, updates)
            total.rows += len(updates)

        try:
            if execute:
                async with conn.transaction():
                    await apply_batch()
            else:
                await apply_batch()
        except Exception:
            logger.error(
                f"{spec.table}: batch of rows {start + 1}-{start + len(batch)} failed and was "
                f"rolled back. Earlier batches are already committed; re-running is safe."
            )
            raise

        logger.info(f"{spec.table}: {min(start + batch_size, len(ids))}/{len(ids)} rows "
                    f"processed ({time.monotonic() - batch_started:.1f}s)")


# --- Input ---------------------------------------------------------------------------------

HEADER_MAP = {
    "sno": "s_no",
    "scormid": "old_id",
    "scormcoursename": "old_name",
    "nonscormid": "new_id",
    "nonscormcoursename": "new_name",
}


def _norm_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def read_mappings(path: Path) -> List[Mapping]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None) or []
        columns = {HEADER_MAP[_norm_header(h)]: i
                   for i, h in enumerate(header) if _norm_header(h) in HEADER_MAP}
        missing = {"old_id", "new_id"} - set(columns)
        if missing:
            raise SystemExit(
                f"{path}: expected 'SCORM_ID' and 'Non-SCORM ID' columns, found {header}"
            )

        mappings = []
        for line_no, values in enumerate(reader, start=2):
            def get(key):
                i = columns.get(key)
                return values[i].strip() if i is not None and i < len(values) else ""

            if not any(v.strip() for v in values):
                continue
            mappings.append(Mapping(
                line_no=line_no, s_no=get("s_no"),
                old_id=get("old_id"), old_name=get("old_name"),
                new_id=get("new_id"), new_name=get("new_name"),
            ))
        return mappings


def validate(mappings: List[Mapping]) -> Dict[int, Tuple[str, str]]:
    """{line_no: (status, reason)} for every row that must not be processed."""
    skipped: Dict[int, Tuple[str, str]] = {}
    first_seen: Dict[str, Mapping] = {}
    for m in mappings:
        if not CONTENT_ID_RE.match(m.old_id):
            skipped[m.line_no] = ("skipped_invalid_scorm_id", f"SCORM_ID {m.old_id!r}")
        elif not CONTENT_ID_RE.match(m.new_id):
            skipped[m.line_no] = (
                "skipped_no_new_id", f"Non-SCORM ID {m.new_id!r} ({m.new_name or 'blank'})"
            )
        elif m.old_id == m.new_id:
            skipped[m.line_no] = ("skipped_same_id", "SCORM_ID equals Non-SCORM ID")
        elif m.old_id in first_seen:
            earlier = first_seen[m.old_id]
            if earlier.new_id == m.new_id:
                skipped[m.line_no] = (
                    "skipped_duplicate_row", f"same mapping as line {earlier.line_no}"
                )
            else:
                skipped[m.line_no] = (
                    "skipped_conflicting_row",
                    f"line {earlier.line_no} maps this SCORM_ID to {earlier.new_id}",
                )
        else:
            first_seen[m.old_id] = m

    # old -> new -> newer would depend on row order; refuse rather than guess.
    for m in first_seen.values():
        if m.line_no not in skipped and m.new_id in first_seen:
            skipped[m.line_no] = (
                "skipped_chained_mapping",
                f"Non-SCORM ID is the SCORM_ID of line {first_seen[m.new_id].line_no}",
            )
    return skipped


async def fetch_course_metadata(conn, identifiers: List[str]) -> Dict[str, dict]:
    if not identifiers:
        return {}
    rows = await conn.fetch(COURSE_METADATA_SQL, identifiers)
    return {row["identifier"]: dict(row) for row in rows}


# --- Output --------------------------------------------------------------------------------

REPORT_COLUMNS = (
    ["line_no", "s_no", "scorm_id", "scorm_course_name", "non_scorm_id",
     "non_scorm_course_name", "mode", "status", "reason", "metadata_found"]
    + [f"{t}_{suffix}" for t in TABLES
       for suffix in ("rows_updated", "entries_replaced", "entries_removed")]
    + ["total_rows_updated"]
)


def configure_logging(log_file: Path) -> None:
    """Log file gets everything including the per-entry OLD/NEW objects (DEBUG); the terminal
    gets progress and summary only (INFO)."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.info("Logging initialized at %s", log_file)


def _report_row(m: Mapping, mode: str, status: str, reason: str, metadata_found: str,
                per_table: Optional[Dict[str, TableResult]]) -> Dict[str, Any]:
    row = {
        "line_no": m.line_no, "s_no": m.s_no,
        "scorm_id": m.old_id, "scorm_course_name": m.old_name,
        "non_scorm_id": m.new_id, "non_scorm_course_name": m.new_name,
        "mode": mode, "status": status, "reason": reason, "metadata_found": metadata_found,
    }
    per_table = per_table or {t: TableResult() for t in TABLES}
    for t in TABLES:
        row[f"{t}_rows_updated"] = per_table[t].rows
        row[f"{t}_entries_replaced"] = per_table[t].replaced
        row[f"{t}_entries_removed"] = per_table[t].removed
    row["total_rows_updated"] = sum(r.rows for r in per_table.values())
    return row


def write_report(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def build_report(mappings, skipped, actions, metadata, stats, mode):
    """One report row per input row, in file order; logs a one-line outcome for each."""
    rows = []
    for m in mappings:
        prefix = f"line {m.line_no} {m.old_id} -> {m.new_id or '-'}"
        if m.line_no in skipped:
            status, reason = skipped[m.line_no]
            logger.info(f"{prefix}: {status} ({reason})")
            rows.append(_report_row(m, mode, status, reason, "", None))
            continue

        metadata_found = "yes" if m.new_id in metadata else "no"
        if m.old_id not in actions:  # no metadata and --on-missing-metadata=skip
            logger.info(f"{prefix}: skipped_no_metadata ({REASON_NO_METADATA})")
            rows.append(_report_row(
                m, mode, "skipped_no_metadata", REASON_NO_METADATA, metadata_found, None
            ))
            continue

        per_table = stats[m.old_id]
        if not any(r.rows for r in per_table.values()):
            status, reason = "no_match", ""
        elif actions[m.old_id].remove_only:
            status, reason = "removed_no_metadata", REASON_NO_METADATA
        elif metadata_found == "no":
            status, reason = "replaced", "no metadata - name from CSV, other fields from old course"
        else:
            status, reason = "replaced", ""
        logger.info(f"{prefix}: {status}  " + "  ".join(
            f"{t}={per_table[t].rows}" for t in TABLES
        ) + (f"  ({reason})" if reason else ""))
        rows.append(_report_row(m, mode, status, reason, metadata_found, per_table))
    return rows


# --- Main ----------------------------------------------------------------------------------

def _parse_args():
    parser = argparse.ArgumentParser(
        description="Replace SCORM course identifiers with their non-SCORM replacement in "
                    "recommended_courses, suggested_courses, cbp_plans and approval_request_items.",
    )
    parser.add_argument("--csv", type=Path, required=True,
                        help="Mapping CSV with 'SCORM_ID' and 'Non-SCORM ID' columns")
    parser.add_argument("--execute", action="store_true",
                        help="Commit the changes. Without this the run is a dry run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Explicitly request a dry run (the default)")
    parser.add_argument("--approval-statuses", default=None,
                        help="Comma-separated approval_request_items statuses to update, e.g. "
                             "'pending,approved' (default: all statuses)")
    parser.add_argument("--on-missing-metadata", choices=("remove", "replace", "skip"),
                        default="remove",
                        help="When the new course is not in course_metadata_weightage: "
                             "'remove' the old course without adding the new one (default), "
                             "'replace' it anyway using the CSV name, or 'skip' the row")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Rows rewritten per batch / transaction "
                             f"(default: {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--log-entries", type=int, default=DEFAULT_LOG_ENTRIES,
                        help="OLD/NEW objects logged per mapping per table "
                             "(default: -1 = every change, which can be gigabytes in "
                             "production; 0 = none; N = first N)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Outcome CSV path (default: next to the input, "
                             "<input>_<dryrun|execute>_<timestamp>.csv)")
    args = parser.parse_args()

    if args.execute and args.dry_run:
        parser.error("--execute and --dry-run are mutually exclusive")
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")
    if args.log_entries < -1:
        parser.error("--log-entries must be -1 (all), 0 (none) or a positive number")

    if args.approval_statuses:
        statuses = [s.strip().lower() for s in args.approval_statuses.split(",") if s.strip()]
        unknown = set(statuses) - APPROVAL_STATUSES
        if unknown:
            parser.error(f"unknown --approval-statuses {sorted(unknown)}; "
                         f"allowed: {sorted(APPROVAL_STATUSES)}")
        args.approval_statuses = statuses
    return args


async def main() -> None:
    global _log_entries_limit
    args = _parse_args()
    _log_entries_limit = args.log_entries
    csv_path = args.csv.expanduser()
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"replace_scorm_courses_{mode.lower()}_{RUN_TIMESTAMP}.log"
    out_path = args.out or csv_path.resolve().parent / (
        f"{csv_path.stem}_{mode.lower().replace('-', '')}_{RUN_TIMESTAMP}.csv"
    )
    configure_logging(log_file)

    mappings = read_mappings(csv_path)
    if not mappings:
        raise SystemExit(f"No data rows found in {csv_path}.")
    skipped = validate(mappings)
    to_process = [m for m in mappings if m.line_no not in skipped]

    logger.info(f"Mode: {mode}")
    logger.info(f"Rows read: {len(mappings)}  to process: {len(to_process)}  "
                f"skipped by validation: {len(skipped)}")
    logger.info(f"On missing metadata: {args.on_missing_metadata}  "
                f"Batch size: {args.batch_size}  Log entries per mapping: "
                f"{'all' if args.log_entries < 0 else args.log_entries}")
    logger.info("approval_request_items statuses: "
                + (", ".join(args.approval_statuses) if args.approval_statuses else "all"))

    stats: Dict[str, Dict[str, TableResult]] = defaultdict(
        lambda: {t: TableResult() for t in TABLES}
    )
    totals = {t: TableResult() for t in TABLES}
    run_started = time.monotonic()

    conn = await asyncpg.connect(_asyncpg_dsn(_require_env("DATABASE_URL")))
    try:
        await _init_connection(conn)
        new_ids = sorted({m.new_id for m in to_process})
        metadata = await fetch_course_metadata(conn, new_ids)
        logger.info(f"New courses found in course_metadata_weightage: "
                    f"{len(metadata)}/{len(new_ids)}")

        actions: Dict[str, Action] = {}
        for m in to_process:
            meta = metadata.get(m.new_id)
            if meta is None and args.on_missing_metadata == "skip":
                continue
            actions[m.old_id] = Action(
                m, meta, remove_only=meta is None and args.on_missing_metadata == "remove"
            )

        if actions:
            for spec in TABLE_SPECS:
                await process_table(
                    conn, spec, actions, stats, totals,
                    args.execute, args.batch_size, args.approval_statuses,
                )
    finally:
        await conn.close()

    report_rows = build_report(mappings, skipped, actions, metadata, stats, mode)
    write_report(out_path, report_rows)

    statuses = Counter(r["status"] for r in report_rows)
    verb = "Updated" if args.execute else "Would update"
    logger.info("")
    logger.info(f"Row outcomes: {dict(statuses)}")
    logger.info(f"{verb} per table (rows / entries replaced / entries removed):")
    for t in TABLES:
        r = totals[t]
        logger.info(f"  {t:<24} {r.rows:>7} / {r.replaced:>7} / {r.removed:>7}")
    logger.info(f"Finished in {time.monotonic() - run_started:.1f}s")
    logger.info(f"Outcome CSV written to: {out_path}")
    logger.info(f"Log written to: {log_file}")
    logger.info("Committed." if args.execute
                else "Dry run - nothing was changed. Re-run with --execute to apply.")


if __name__ == "__main__":
    asyncio.run(main())

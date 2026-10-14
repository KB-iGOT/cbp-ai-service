"""
Replace retired SCORM course references with their new non-SCORM course across the CBP database.

The old SCORM content has been deleted from iGOT and a non-SCORM copy published under a new
identifier. Every place the old identifier is stored is pointed at the new one instead, in:

    1. recommended_courses      filtered_courses[].identifier, actual_courses[].identifier
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

Rows are processed one at a time, in file order. Each CSV row is one transaction covering all four
tables -- either all four are updated for that row or none are -- and under --execute the rows
being edited are locked (SELECT ... FOR UPDATE) so a concurrent app edit cannot be clobbered. A row
that fails is rolled back on its own and reported as 'error'; the run carries on.

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

Output: a log under scripts/logs/ and an outcome CSV (one row per input row, with rows updated and
entries replaced per table) next to the input file unless --out is given. The terminal ends with a
per-table summary of rows impacted.

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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

# A real content identifier; '-' / 'Upload Pending' / blanks fail this and the row is skipped.
CONTENT_ID_RE = re.compile(r"^do_\w+$")

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
class TableResult:
    rows: int = 0         # DB rows rewritten
    replaced: int = 0     # entries whose identifier was swapped to the new course
    removed: int = 0      # old entries removed without a replacement (duplicate / no metadata)
    row_ids: Set[str] = field(default_factory=set)


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


REASON_DUPLICATE = "new course already in list"
REASON_NO_METADATA = "new course not in course_metadata_weightage"


def _log_change(where: Optional[str], old, new=None, reason: str = "") -> None:
    """Log one entry's before/after. `new` is None for an old entry removed without replacement."""
    if where is None:
        return
    if new is None:
        logger.info(f"    {where}: REMOVED ({reason}) OLD {_dumps(old)}")
    else:
        logger.info(f"    {where}:\n      OLD {_dumps(old)}\n      NEW {_dumps(new)}")


def replace_in_courses(
    courses, m: Mapping, meta: Optional[dict], where: Optional[str] = None,
    remove_only: bool = False,
) -> Tuple[Any, int, int]:
    """Swap the old course for the new one in a list of course objects; with `remove_only` the
    old course is just removed. Returns (new_list, replaced, removed); a non-list value is passed
    through untouched. Each change is logged against `where` (table/row/column) when given."""
    if not isinstance(courses, list):
        return courses, 0, 0
    has_new = any(_identifier_of(c) == m.new_id for c in courses)
    result, replaced, removed = [], 0, 0
    for course in courses:
        if _identifier_of(course) != m.old_id:
            result.append(course)
        elif remove_only or has_new:
            _log_change(where, course,
                        reason=REASON_NO_METADATA if remove_only else REASON_DUPLICATE)
            removed += 1
        else:
            new_course = _rebuild_entry(course, m, meta)
            _log_change(where, course, new_course)
            result.append(new_course)
            replaced += 1
            has_new = True  # any further copy of the old id would now be a duplicate
    return result, replaced, removed


def replace_in_identifiers(
    ids, m: Mapping, where: Optional[str] = None, remove_only: bool = False
) -> Tuple[Any, int, int]:
    """Same as replace_in_courses for an array of bare identifier strings."""
    if not isinstance(ids, list):
        return ids, 0, 0
    has_new = any(str(i).strip() == m.new_id for i in ids)
    result, replaced, removed = [], 0, 0
    for value in ids:
        if str(value).strip() != m.old_id:
            result.append(value)
        elif remove_only or has_new:
            _log_change(where, value,
                        reason=REASON_NO_METADATA if remove_only else REASON_DUPLICATE)
            removed += 1
        else:
            _log_change(where, value, m.new_id)
            result.append(m.new_id)
            replaced += 1
            has_new = True
    return result, replaced, removed


def replace_in_plan_data(
    data, m: Mapping, meta: Optional[dict], where: Optional[str] = None,
    remove_only: bool = False,
) -> Tuple[Any, int, int]:
    """cbp_plan_data is a list of CBP plan snapshots (a single snapshot object is tolerated, as
    the publish script does), each with its own selected_courses list."""
    plans = data if isinstance(data, list) else [data]
    new_plans, replaced, removed = [], 0, 0
    for index, plan in enumerate(plans):
        if isinstance(plan, dict) and isinstance(plan.get("selected_courses"), list):
            plan_where = None if where is None else f"{where}[{index}].selected_courses"
            courses, r, d = replace_in_courses(
                plan["selected_courses"], m, meta, plan_where, remove_only
            )
            if r or d:
                plan = {**plan, "selected_courses": courses}
            replaced += r
            removed += d
        new_plans.append(plan)
    return (new_plans if isinstance(data, list) else new_plans[0]), replaced, removed


# --- One function per table, run in the order listed in the module docstring ---------------

def _lock(execute: bool) -> str:
    return "FOR UPDATE" if execute else ""


async def replace_in_recommended_courses(conn, m, meta, execute, remove_only) -> TableResult:
    probe = [{"identifier": m.old_id}]
    rows = await conn.fetch(
        f"""SELECT id, filtered_courses, actual_courses
              FROM recommended_courses
             WHERE filtered_courses @> $1::jsonb OR actual_courses @> $1::jsonb
             {_lock(execute)}""",
        probe,
    )
    result = TableResult()
    for row in rows:
        where = f"recommended_courses id={row['id']}"
        filtered, r1, d1 = replace_in_courses(
            row["filtered_courses"], m, meta, f"{where} filtered_courses", remove_only
        )
        actual, r2, d2 = replace_in_courses(
            row["actual_courses"], m, meta, f"{where} actual_courses", remove_only
        )
        if not (r1 or d1 or r2 or d2):
            continue
        if execute:
            await conn.execute(
                """UPDATE recommended_courses
                      SET filtered_courses = $2::jsonb, actual_courses = $3::jsonb,
                          updated_at = now()
                    WHERE id = $1""",
                row["id"], filtered, actual,
            )
        result.rows += 1
        result.replaced += r1 + r2
        result.removed += d1 + d2
        result.row_ids.add(str(row["id"]))
    return result


async def replace_in_suggested_courses(conn, m, meta, execute, remove_only) -> TableResult:
    rows = await conn.fetch(
        f"""SELECT id, course_identifiers
              FROM suggested_courses
             WHERE course_identifiers @> $1::jsonb
             {_lock(execute)}""",
        [m.old_id],
    )
    result = TableResult()
    for row in rows:
        ids, replaced, removed = replace_in_identifiers(
            row["course_identifiers"], m,
            f"suggested_courses id={row['id']} course_identifiers", remove_only,
        )
        if not (replaced or removed):
            continue
        if execute:
            await conn.execute(
                """UPDATE suggested_courses
                      SET course_identifiers = $2::jsonb, updated_at = now()
                    WHERE id = $1""",
                row["id"], ids,
            )
        result.rows += 1
        result.replaced += replaced
        result.removed += removed
        result.row_ids.add(str(row["id"]))
    return result


async def replace_in_cbp_plans(conn, m, meta, execute, remove_only) -> TableResult:
    rows = await conn.fetch(
        f"""SELECT id, selected_courses
              FROM cbp_plans
             WHERE selected_courses @> $1::jsonb
             {_lock(execute)}""",
        [{"identifier": m.old_id}],
    )
    result = TableResult()
    for row in rows:
        courses, replaced, removed = replace_in_courses(
            row["selected_courses"], m, meta, f"cbp_plans id={row['id']} selected_courses",
            remove_only,
        )
        if not (replaced or removed):
            continue
        if execute:
            await conn.execute(
                """UPDATE cbp_plans
                      SET selected_courses = $2::jsonb, updated_at = now()
                    WHERE id = $1""",
                row["id"], courses,
            )
        result.rows += 1
        result.replaced += replaced
        result.removed += removed
        result.row_ids.add(str(row["id"]))
    return result


async def replace_in_approval_request_items(
    conn, m, meta, execute, remove_only, statuses: Optional[List[str]]
) -> TableResult:
    """cbp_plan_data is JSON rather than JSONB, so it is read as jsonb to be searched and written
    back as json. The table has no updated_at column."""
    course = {"identifier": m.old_id}
    rows = await conn.fetch(
        f"""SELECT id, cbp_plan_data::jsonb AS data
              FROM approval_request_items
             WHERE (cbp_plan_data::jsonb @> $1::jsonb OR cbp_plan_data::jsonb @> $2::jsonb)
               AND ($3::text[] IS NULL OR lower(status::text) = ANY($3::text[]))
             {_lock(execute)}""",
        [{"selected_courses": [course]}],
        {"selected_courses": [course]},
        statuses,
    )
    result = TableResult()
    for row in rows:
        data, replaced, removed = replace_in_plan_data(
            row["data"], m, meta, f"approval_request_items id={row['id']} cbp_plan_data",
            remove_only,
        )
        if not (replaced or removed):
            continue
        if execute:
            await conn.execute(
                "UPDATE approval_request_items SET cbp_plan_data = $2::json WHERE id = $1",
                row["id"], data,
            )
        result.rows += 1
        result.replaced += replaced
        result.removed += removed
        result.row_ids.add(str(row["id"]))
    return result


async def _replace_everywhere(
    conn, m, meta, execute, remove_only, statuses
) -> Dict[str, TableResult]:
    args = (conn, m, meta, execute, remove_only)
    return {
        "recommended_courses": await replace_in_recommended_courses(*args),
        "suggested_courses": await replace_in_suggested_courses(*args),
        "cbp_plans": await replace_in_cbp_plans(*args),
        "approval_request_items": await replace_in_approval_request_items(*args, statuses),
    }


async def process_mapping(
    conn, m, meta, execute, remove_only, statuses
) -> Dict[str, TableResult]:
    """All four tables for one mapping; under --execute in a single transaction."""
    if execute:
        async with conn.transaction():
            return await _replace_everywhere(conn, m, meta, execute, remove_only, statuses)
    return await _replace_everywhere(conn, m, meta, execute, remove_only, statuses)


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
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler(sys.stdout)
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
    parser.add_argument("--out", type=Path, default=None,
                        help="Outcome CSV path (default: next to the input, "
                             "<input>_<dryrun|execute>_<timestamp>.csv)")
    args = parser.parse_args()

    if args.execute and args.dry_run:
        parser.error("--execute and --dry-run are mutually exclusive")

    if args.approval_statuses:
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
    logger.info(f"On missing metadata: {args.on_missing_metadata}")
    logger.info("approval_request_items statuses: "
                + (", ".join(args.approval_statuses) if args.approval_statuses else "all"))

    report_rows: List[Dict[str, Any]] = []
    totals = {t: TableResult() for t in TABLES}

    conn = await asyncpg.connect(_asyncpg_dsn(_require_env("DATABASE_URL")))
    try:
        await _init_connection(conn)
        metadata = await fetch_course_metadata(conn, sorted({m.new_id for m in to_process}))
        logger.info(f"New courses found in course_metadata_weightage: {len(metadata)}/"
                    f"{len({m.new_id for m in to_process})}")

        total = len(mappings)
        for index, m in enumerate(mappings, start=1):
            prefix = f"[{index}/{total}] line {m.line_no} {m.old_id} -> {m.new_id or '-'}"
            if m.line_no in skipped:
                status, reason = skipped[m.line_no]
                logger.info(f"{prefix}: {status} ({reason})")
                report_rows.append(_report_row(m, mode, status, reason, "", None))
                continue

            meta = metadata.get(m.new_id)
            metadata_found = "yes" if meta else "no"
            if meta is None and args.on_missing_metadata == "skip":
                logger.warning(f"{prefix}: skipped_no_metadata ({REASON_NO_METADATA})")
                report_rows.append(_report_row(
                    m, mode, "skipped_no_metadata", REASON_NO_METADATA, metadata_found, None
                ))
                continue
            remove_only = meta is None and args.on_missing_metadata == "remove"

            try:
                per_table = await process_mapping(
                    conn, m, meta, args.execute, remove_only, args.approval_statuses
                )
            except Exception as exc:
                logger.exception(f"{prefix}: error - rolled back, nothing changed for this row")
                report_rows.append(_report_row(m, mode, "error", str(exc), metadata_found, None))
                continue

            changed = any(r.rows for r in per_table.values())
            if not changed:
                status, reason = "no_match", ""
            elif remove_only:
                status, reason = "removed_no_metadata", REASON_NO_METADATA
            elif meta is None:
                status, reason = "replaced", "no metadata - name from CSV, other fields from old course"
            else:
                status, reason = "replaced", ""
            for t in TABLES:
                totals[t].rows += per_table[t].rows
                totals[t].replaced += per_table[t].replaced
                totals[t].removed += per_table[t].removed
                totals[t].row_ids |= per_table[t].row_ids
            logger.info(f"{prefix}: {status}  " + "  ".join(
                f"{t}={per_table[t].rows}" for t in TABLES
            ) + (f"  ({reason})" if reason else ""))
            report_rows.append(_report_row(m, mode, status, reason, metadata_found, per_table))
    finally:
        await conn.close()

    write_report(out_path, report_rows)

    statuses = Counter(r["status"] for r in report_rows)
    verb = "Updated" if args.execute else "Would update"
    logger.info("")
    logger.info(f"Row outcomes: {dict(statuses)}")
    logger.info(f"{verb} per table (distinct rows / row updates / entries replaced / "
                f"entries removed):")
    for t in TABLES:
        r = totals[t]
        logger.info(f"  {t:<24} {len(r.row_ids):>6} / {r.rows:>6} / {r.replaced:>6} / "
                    f"{r.removed:>6}")
    logger.info(f"Outcome CSV written to: {out_path}")
    logger.info(f"Log written to: {log_file}")
    logger.info("Committed." if args.execute
                else "Dry run - nothing was changed. Re-run with --execute to apply.")


if __name__ == "__main__":
    asyncio.run(main())

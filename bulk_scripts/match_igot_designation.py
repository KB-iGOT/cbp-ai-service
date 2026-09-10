"""
Bulk iGOT designation matcher for existing role_mappings rows.

Reuses the exact same logic the v3 role-mapping API uses to auto-match a designation against
the iGOT master list (see src/api/v3/role_mappings.py's "Auto-match all completed designations
against iGOT master" step): designation_matcher_service.match() -- exact match via the iGOT
/api/designation/search HTTP API, falling back to semantic match (gemini-embedding-2 +
pgvector cosine similarity, threshold settings.DESIGNATION_SIMILARITY_THRESHOLD) for anything
not matched exactly.

Input file: .csv or .xlsx/.xlsm. ONE column is mandatory:
    id    -> role_mappings.id (UUID) of the row to match

Any other columns in the file are ignored. The row's designation_name is read from the DB
(role_mappings.designation_name for that id) -- not from the file.

For each input id:
  - the role_mappings row must exist for --user-id, else `not_found`
  - designation_name must be non-empty, else `no_designation`
  - the match result (exact or semantic) is looked up via designation_matcher_service.match()
  - on a match, igot_designation_name / igot_designation_id are updated on that row
  - no match found -> `no_match` (not fatal to the rest of the run)

Dry-run by default (no writes) -- pass --execute to persist. Every run (dry-run and --execute)
writes a log + an outcome CSV under bulk_scripts/logs/, named after the input file (same
convention as the other bulk_scripts).

Run (dry-run):
    python bulk_scripts/match_igot_designation.py --input rows.csv --user-id <uuid>
Run (persist):
    python bulk_scripts/match_igot_designation.py --input rows.csv --user-id <uuid> --execute
Run with a specific batch size (default 10):
    python bulk_scripts/match_igot_designation.py --input rows.xlsx --user-id <uuid> --execute --batch-size 20
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import pkgutil
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── path bootstrap ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.core.configs import settings  # noqa: E402  (loads .env)

if settings.GOOGLE_APPLICATION_CREDENTIALS:
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", settings.GOOGLE_APPLICATION_CREDENTIALS)

# Register ALL SQLAlchemy mappers (the server imports every model at startup).
import src.models  # noqa: E402
for _m in pkgutil.iter_modules(src.models.__path__):
    __import__(f"src.models.{_m.name}")

from src.core.database import sessionmanager  # noqa: E402
from src.crud.role_mapping import crud_role_mapping  # noqa: E402
from src.services.designation_matcher_service import designation_matcher_service  # noqa: E402

# Import the app logger up front: it runs logging.config.fileConfig() which disables existing
# loggers + replaces handlers. Triggering it now (cached) means our setup_logging() sticks.
import src.core.logger  # noqa: E402,F401

log = logging.getLogger("igot_designation_matcher")

SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

DEFAULT_BATCH_SIZE = 10

REPORT_COLUMNS = [
    "row_no",
    "status",
    "reason",
    "role_mapping_id",
    "designation_name",
    "prior_igot_designation_name",
    "prior_igot_designation_id",
    "match_type",
    "similarity_score",
    "new_igot_designation_name",
    "new_igot_designation_id",
]

_SCI_NOTATION_RE = re.compile(r"^[-+]?\d*\.?\d+[eE][-+]?\d+$")


def setup_logging(path: Path) -> None:
    """Console + file (one fresh file per run). Installed after the app's fileConfig."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    con = logging.StreamHandler(sys.stdout)
    con.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(con)
    root.addHandler(fh)
    root.setLevel(logging.INFO)
    logging.getLogger("google").setLevel(logging.WARNING)
    log.info(f"Logging initialized at {path}")


def _clean(value):
    """Normalize a cell to a stripped string, or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _looks_scientific(value) -> bool:
    return bool(value) and bool(_SCI_NOTATION_RE.match(str(value).strip()))


def read_rows(path: str) -> list[dict]:
    """Read the input file into a list of dict rows. .xlsx/.xlsm via openpyxl; everything else
    as delimited text with the delimiter auto-detected (tab for a spreadsheet paste, comma for a
    saved CSV). Headers are lowercased/stripped so `id`, `Id`, ` ID ` all resolve the same."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [str(h).strip().lower() if h is not None else "" for h in next(rows_iter)]
        except StopIteration:
            return []
        rows = []
        for values in rows_iter:
            if values is None or all(v is None for v in values):
                continue
            rows.append(
                {header[i]: (values[i] if i < len(values) else None) for i in range(len(header))}
            )
        return rows

    with open(path, newline="", encoding="utf-8-sig") as f:
        first_line = f.readline()
        f.seek(0)
        delimiter = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        rows = []
        for raw in reader:
            row = {(k or "").strip().lower(): v for k, v in raw.items()}
            if all(_clean(v) is None for v in row.values()):
                continue
            rows.append(row)
        return rows


async def match_one(row: dict, row_no: int, execute: bool, user_uuid: uuid.UUID) -> dict:
    """Process one input row (one role_mappings.id). Returns a uniform result dict."""
    result = {
        "row_no": row_no,
        "status": None,
        "reason": "",
        "role_mapping_id": "",
        "designation_name": "",
        "prior_igot_designation_name": "",
        "prior_igot_designation_id": "",
        "match_type": "",
        "similarity_score": "",
        "new_igot_designation_name": "",
        "new_igot_designation_id": "",
    }

    def done(status, reason=""):
        result["status"] = status
        result["reason"] = reason
        return result

    raw_id = _clean(row.get("id"))
    if not raw_id:
        log.error(f"Row {row_no}: SKIP - missing 'id' column value")
        return done("error", "missing_id")

    if _looks_scientific(raw_id):
        log.error(f"Row {row_no}: SKIP - 'id' looks like scientific notation ({raw_id!r})")
        return done("error", "id_scientific_notation")

    try:
        role_mapping_id = uuid.UUID(raw_id)
    except ValueError:
        log.error(f"Row {row_no}: SKIP - 'id' is not a valid UUID ({raw_id!r})")
        return done("error", "invalid_uuid")

    result["role_mapping_id"] = str(role_mapping_id)

    async with sessionmanager.session() as db:
        rm = await crud_role_mapping.get_by_id_and_user(db, role_mapping_id, user_uuid)

    if rm is None:
        log.warning(f"Row {row_no}: SKIP - role_mappings id={role_mapping_id} not found for user {user_uuid}")
        return done("not_found", "no role_mappings row for this id + user_id")

    designation_name = _clean(rm.designation_name)
    result["designation_name"] = designation_name or ""
    result["prior_igot_designation_name"] = rm.igot_designation_name or ""
    result["prior_igot_designation_id"] = rm.igot_designation_id or ""

    if not designation_name:
        log.warning(f"Row {row_no}: SKIP - role_mappings id={role_mapping_id} has empty designation_name")
        return done("no_designation", "designation_name is empty")

    async with sessionmanager.session() as db:
        match_results = await designation_matcher_service.match(db, [designation_name])

    match_data = next(
        (m for m in match_results if (m.get("input_designation") or "").lower() == designation_name.lower()),
        None,
    )

    if not match_data or not match_data.get("id"):
        log.info(f"Row {row_no}: NO MATCH for '{designation_name}' (role_mapping_id={role_mapping_id})")
        return done("no_match", "no exact or similar iGOT designation found")

    result["match_type"] = match_data.get("match_type", "")
    result["similarity_score"] = match_data.get("similarity_score", "")
    result["new_igot_designation_name"] = match_data.get("designation", "")
    result["new_igot_designation_id"] = match_data.get("id", "")

    if not execute:
        log.info(
            f"Row {row_no}: DRY-RUN would set igot_designation_name={match_data.get('designation')!r}, "
            f"igot_designation_id={match_data.get('id')!r} on role_mapping_id={role_mapping_id} "
            f"(match_type={match_data.get('match_type')})"
        )
        return done("would_update")

    async with sessionmanager.session() as db:
        updated = await crud_role_mapping.bulk_update_designation_matching(
            db,
            [{
                "role_mapping_id": role_mapping_id,
                "igot_designation_name": match_data.get("designation"),
                "igot_designation_id": match_data.get("id"),
            }],
        )

    if not updated:
        log.error(f"Row {row_no}: update affected 0 rows for role_mapping_id={role_mapping_id}")
        return done("error", "update affected 0 rows")

    log.info(
        f"Row {row_no}: UPDATED role_mapping_id={role_mapping_id} -> "
        f"igot_designation_name={match_data.get('designation')!r}, igot_designation_id={match_data.get('id')!r} "
        f"(match_type={match_data.get('match_type')})"
    )
    return done("updated")


def write_report_csv(results: list[dict], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            writer.writerow(["" if r.get(col) is None else str(r.get(col, "")) for col in REPORT_COLUMNS])


async def main():
    parser = argparse.ArgumentParser(
        description="Match existing role_mappings rows against the iGOT master designation list "
                    "(exact + semantic), updating igot_designation_name/igot_designation_id."
    )
    parser.add_argument("--input", required=True, help="Path to the CSV/Excel file with a mandatory 'id' column")
    parser.add_argument("--user-id", required=True, type=uuid.UUID, help="Owner user's UUID (role_mappings.user_id)")
    parser.add_argument("--execute", action="store_true", help="Persist changes. Default is a dry-run (no writes).")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="How many rows to process concurrently (default: 10).")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"Input file not found: {args.input}")

    input_stem = Path(args.input).stem
    log_file = LOGS_DIR / f"{input_stem}_{RUN_TIMESTAMP}.log"
    outcome_csv_file = Path(args.input).resolve().parent / f"{input_stem}_{RUN_TIMESTAMP}.csv"
    setup_logging(log_file)

    rows = read_rows(args.input)
    if not rows:
        sys.exit("Input file has no data rows.")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log.info(f"Loaded {len(rows)} row(s) from {args.input}. Mode: {mode}. user_id={args.user_id}")

    sessionmanager.init(str(settings.DATABASE_URL))
    results: list[dict] = [None] * len(rows)  # keep results in input order
    try:
        semaphore = asyncio.Semaphore(args.batch_size)

        async def process(index: int, row: dict):
            async with semaphore:
                log.info(f"--- Row {index}/{len(rows)} ---")
                try:
                    res = await match_one(row, index, args.execute, args.user_id)
                except Exception as e:
                    log.exception(f"Row {index} failed")
                    res = {"row_no": index, "status": "error", "reason": f"unexpected: {e}"}
                results[index - 1] = res

        await asyncio.gather(*(process(i, row) for i, row in enumerate(rows, start=1)))
    finally:
        await sessionmanager.close()

    results = [r for r in results if r is not None]

    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    log.info(f"Summary: {summary}")

    write_report_csv(results, outcome_csv_file)
    log.info(f"Outcome CSV written to: {outcome_csv_file}")
    log.info(f"Detailed logs written to: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())

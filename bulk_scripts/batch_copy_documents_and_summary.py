#!/usr/bin/env python3
"""Copy documents (and their summaries) from a source state/department scope to a
target state/department scope, for one target user.

Driven by an input file (.csv or .xlsx) where each data row gives one scope-pair:
    source_state_center_id, source_department_id, target_state_center_id, target_department_id

For each row: every document in the documents table whose (state_center_id, department_id)
matches the source scope -- regardless of who uploaded it -- is copied. The copy's GCS file
is duplicated to a brand-new object path, and a brand-new documents row is inserted with
state_center_id/department_id set to the row's target scope and uploader_id set to --user-id.

Unlike batch_copy_all_documents.py, the summary fields (summary_status, summary_text,
summary_error, last_summary_request_id) are carried over AS-IS from the source document --
this script is explicitly for moving an already-summarized document into a new scope without
regenerating its summary.

Same dedup rule as batch_copy_all_documents.py: within one row's source scope, if multiple
documents share the same filename (uploaded by different people), only the most recently
created one is copied -- see dedupe_documents_by_filename.

A source document is also skipped (status=skipped_already_in_target) if its filename already
exists among the documents in the row's TARGET scope FOR --user-id specifically -- so re-running
this script (or two rows pointing at the same target scope for the same user) never creates
duplicate copies for that user. A same-named document owned by a different user at that target
scope does not trigger this skip.

Fully self-contained: does NOT import anything from this repo's src/ (no app config, no ORM
models). Talks to Postgres directly via asyncpg and to GCS directly via google-cloud-storage.

DATABASE_URL, GCS bucket/credentials/prefix are all read from the repo's .env file (see
ENV_FILE below) -- nothing is passed on the command line except the input file, --user-id,
and --batch-size.

DRY RUN BY DEFAULT: without --execute, no DB/GCS writes happen -- only a plan is computed. An
outcome CSV (one row per source document found) is written either way, alongside the input
file (named after it), while the log file goes to bulk_scripts/logs/ (also named after the
input file). Pass --execute to perform real copies.

Usage:
    python batch_copy_documents_and_summary.py --input /path/to/scopes.csv --user-id <UUID>
    python batch_copy_documents_and_summary.py --input /path/to/scopes.xlsx --user-id <UUID> --execute
    python batch_copy_documents_and_summary.py --input /path/to/scopes.csv --user-id <UUID> --execute --batch-size 20
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg
from google.cloud import storage

SCRIPT_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("copy_documents_between_scopes")

# --------------------------------------------------------------------------
# CLI args -- --input and --user-id are mandatory; --execute opts into a
# real run (default is dry-run).
# --------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Copy documents (and their existing summaries) from a source state/department "
            "scope to a target state/department scope, for every scope-pair row in a .csv/.xlsx "
            "input file."
        )
    )
    parser.add_argument("--input", required=True,
                        help="Path to the input file: .csv or .xlsx/.xlsm. Must contain columns "
                             "source_state_center_id, source_department_id, target_state_center_id, "
                             "target_department_id.")
    parser.add_argument("--user-id", required=True, type=uuid.UUID,
                        help="Target user UUID (owner of every copied document). Mandatory.")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="How many documents to process concurrently (default: 10).")
    parser.add_argument("--execute", action="store_true",
                        help="Perform real copies. Without this flag, the script only does a dry run "
                             "(no DB writes, no GCS uploads); an outcome CSV is written either way.")
    parser.add_argument("--sheet", default="",
                        help="xlsx: restrict to one worksheet tab (default: read all tabs).")
    return parser.parse_args()


_ARGS = parse_args()

# --------------------------------------------------------------------------
# Script-level configuration derived from CLI args
# --------------------------------------------------------------------------

INPUT_PATH = _ARGS.input
TARGET_USER_ID = _ARGS.user_id
CONCURRENCY = _ARGS.batch_size
SHEET = _ARGS.sheet
DRY_RUN = not _ARGS.execute

ENV_FILE = SCRIPT_DIR / ".." / ".env"

LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Both the log and the outcome CSV are named after the input file (not a fixed script name),
# so multiple runs against different input files are easy to tell apart at a glance.
_INPUT_STEM = Path(INPUT_PATH).stem
LOG_FILE = LOGS_DIR / f"{_INPUT_STEM}_{RUN_TIMESTAMP}.log"

# Outcome CSV is written alongside the input file (not under bulk_scripts/logs/), matching
# batch_generate_and_save_cbp_plan.py's convention -- it sits next to the file the user is
# already tracking for this batch.
OUTCOME_CSV_FILE = Path(INPUT_PATH).resolve().parent / f"{_INPUT_STEM}_{RUN_TIMESTAMP}.csv"


# --------------------------------------------------------------------------
# .env loader (no python-dotenv dependency; standalone)
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Minimal .env parser: KEY=VALUE per line, '#' comments, optional quotes."""
    if not path.exists():
        return
    with open(path, "r") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_env_file(ENV_FILE)


def require_env(name: str) -> str:
    """Fetches a mandatory environment variable, raising a clear, actionable error
    (instead of a bare KeyError) if it's missing from both the environment and the
    .env file -- every config value this script depends on is required, there are
    no silent defaults for connection/credential settings."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Set it in {ENV_FILE} or in the environment before running this script."
        )
    return value


# asyncpg needs a plain postgresql:// DSN -- the app's .env uses the SQLAlchemy-style
# postgresql+asyncpg:// scheme, so the driver suffix is stripped here.
DATABASE_URL = require_env("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://", 1)
GCS_BUCKET = require_env("GCP_STORAGE_BUCKET")
GCS_PREFIX = require_env("GCP_STORAGE_PREFIX")

GCS_CREDENTIALS = require_env("GCP_STORAGE_CREDENTIALS")
if not os.path.isabs(GCS_CREDENTIALS):
    GCS_CREDENTIALS = str((SCRIPT_DIR / ".." / GCS_CREDENTIALS).resolve())


def configure_logging() -> Path:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("Logging initialized at %s", LOG_FILE)
    return LOG_FILE


# --------------------------------------------------------------------------
# Input file loading (.csv or .xlsx/.xlsm) -- scope-pair rows
# --------------------------------------------------------------------------

REQUIRED_COLUMNS = [
    "source_state_center_id", "source_department_id",
    "target_state_center_id", "target_department_id",
]


def _norm(s) -> str:
    return str(s).strip().lower().replace(" ", "_") if s is not None else ""


def _cell(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def load_scope_rows(path: str, sheet: str = "") -> list:
    """Reads the input file's header + data rows into a list of dicts, one per scope-pair
    row, each carrying source_state_center_id/source_department_id/target_state_center_id/
    target_department_id plus its origin (sheet, row_number) for the outcome CSV.

    department_id columns are optional per-row (an empty/NULL department is a valid scope,
    same convention as batch_copy_all_documents.py's "_root_" placeholder); the two
    *_state_center_id columns are mandatory per row."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = [wb[sheet]] if sheet else list(wb.worksheets)
        rows = []
        for ws in sheets:
            it = ws.iter_rows(values_only=True)
            try:
                header_row = next(it)
            except StopIteration:
                continue
            headers = [_norm(h) for h in header_row]
            for rnum, raw in enumerate(it, start=2):
                if raw is None or all(c is None for c in raw):
                    continue
                row = {headers[i]: raw[i] if i < len(raw) else None for i in range(len(headers))}
                row["_sheet"], row["_row"] = ws.title, rnum
                rows.append(row)
        wb.close()
        return rows

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for rnum, raw in enumerate(reader, start=2):
            row = {_norm(k): v for k, v in raw.items()}
            row["_sheet"], row["_row"] = "(csv)", rnum
            rows.append(row)
        return rows


class ScopePair:
    def __init__(self, sheet: str, row_number: int, source_state_center_id: str,
                source_department_id: Optional[str], target_state_center_id: str,
                target_department_id: Optional[str]):
        self.sheet = sheet
        self.row_number = row_number
        self.source_state_center_id = source_state_center_id
        self.source_department_id = source_department_id
        self.target_state_center_id = target_state_center_id
        self.target_department_id = target_department_id


class SkippedScopeRow:
    def __init__(self, sheet: str, row_number: int, reason: str):
        self.sheet = sheet
        self.row_number = row_number
        self.reason = reason


def read_scope_pairs(path: str, sheet: str = ""):
    """Reads every data row into a ScopePair. A row missing either mandatory
    *_state_center_id value is dropped (logged + returned as a SkippedScopeRow so it still
    shows up in the outcome CSV) rather than aborting the whole run."""
    raw_rows = load_scope_rows(path, sheet)
    if not raw_rows:
        raise SystemExit(f"[config] no data rows found in input file: {path}")

    missing_cols = [c for c in REQUIRED_COLUMNS if c not in raw_rows[0]]
    if missing_cols:
        raise SystemExit(
            f"[config] required column(s) {missing_cols} not found in input file: {path}. "
            f"Headers found: {[k for k in raw_rows[0].keys() if not k.startswith('_')]}"
        )

    pairs = []
    skipped = []
    for row in raw_rows:
        sheet_name, row_number = row.get("_sheet", ""), row.get("_row", 0)
        source_state = _cell(row.get("source_state_center_id"))
        target_state = _cell(row.get("target_state_center_id"))
        source_dept = _cell(row.get("source_department_id"))
        target_dept = _cell(row.get("target_department_id"))

        if not source_state or not target_state:
            reason = "missing source_state_center_id or target_state_center_id"
            logger.warning("Row %s (%s): %s -> skipping row", row_number, sheet_name, reason)
            skipped.append(SkippedScopeRow(sheet_name, row_number, reason))
            continue

        pairs.append(ScopePair(
            sheet=sheet_name, row_number=row_number,
            source_state_center_id=source_state, source_department_id=source_dept,
            target_state_center_id=target_state, target_department_id=target_dept,
        ))

    logger.info(
        "Read %s valid scope-pair row(s) from input file (%s skipped due to missing ids): %s",
        len(pairs), len(skipped), path,
    )
    return pairs, skipped


# --------------------------------------------------------------------------
# DB helpers (raw asyncpg -- no ORM)
# --------------------------------------------------------------------------

async def get_documents_in_scope(conn, state_center_id: str, department_id: Optional[str]):
    """Every document row matching this exact (state_center_id, department_id) scope,
    regardless of uploader. department_id=None matches rows where department_id IS NULL."""
    if department_id:
        return await conn.fetch(
            "SELECT * FROM documents WHERE state_center_id = $1 AND department_id = $2 "
            "ORDER BY created_at DESC",
            state_center_id, department_id,
        )
    return await conn.fetch(
        "SELECT * FROM documents WHERE state_center_id = $1 AND department_id IS NULL "
        "ORDER BY created_at DESC",
        state_center_id,
    )


async def get_existing_filenames_in_scope(conn, state_center_id: str, department_id: Optional[str],
                                          uploader_id) -> set:
    """Filenames already present in the target scope FOR THIS TARGET USER -- used to skip
    re-copying a source document whose filename already exists at the target under --user-id, so
    a re-run doesn't keep piling up duplicate copies there. Scoped to uploader_id (not "any
    uploader") since a same-named document owned by a different user at that scope is not this
    user's copy and should not block a fresh one from being created for them."""
    if department_id:
        rows = await conn.fetch(
            "SELECT filename FROM documents WHERE state_center_id = $1 AND department_id = $2 "
            "AND uploader_id = $3",
            state_center_id, department_id, uploader_id,
        )
    else:
        rows = await conn.fetch(
            "SELECT filename FROM documents WHERE state_center_id = $1 AND department_id IS NULL "
            "AND uploader_id = $2",
            state_center_id, uploader_id,
        )
    return {row["filename"] for row in rows}


def dedupe_documents_by_filename(rows):
    """The same document (same filename) can be uploaded by different users under the same
    source scope -- only one copy per filename should ever be copied to the target scope, not
    one per uploader. `rows` must already be ordered by created_at DESC (as
    get_documents_in_scope does) -- dict.setdefault then keeps the FIRST row seen per filename,
    i.e. the most recently created one, and silently drops the rest."""
    seen: dict = {}
    for row in rows:
        seen.setdefault(row["filename"], row)
    deduped = list(seen.values())
    dropped = len(rows) - len(deduped)
    if dropped:
        logger.info(
            "DEDUPE: %s duplicate document(s) skipped (same filename within source scope, "
            "kept the most recently created copy); %s unique document(s) remain",
            dropped, len(deduped),
        )
    return deduped


async def insert_document(conn, **fields):
    await conn.execute(
        """
        INSERT INTO documents (
            file_id, state_center_id, department_id, uploader_id, filename,
            document_type, document_name, stored_path, file_size_bytes,
            summary_status, summary_text, summary_error, last_summary_request_id
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """,
        fields["file_id"], fields["state_center_id"], fields["department_id"],
        fields["uploader_id"], fields["filename"], fields["document_type"],
        fields["document_name"], fields["stored_path"], fields["file_size_bytes"],
        fields["summary_status"], fields["summary_text"], fields["summary_error"],
        fields["last_summary_request_id"],
    )


# --------------------------------------------------------------------------
# GCS helper
# --------------------------------------------------------------------------

def build_blob_name(prefix: str, state_center_id: str, department_id: Optional[str], filename: str) -> str:
    parts = [prefix, state_center_id, department_id if department_id else "_root_", filename]
    return "/".join(parts)


# --------------------------------------------------------------------------
# Core per-document copy logic
# --------------------------------------------------------------------------

async def _copy_one_document(conn, bucket, gcs_prefix, doc, pair: ScopePair, target_user_id, dry_run):
    """Copies one source document into the row's target scope, carrying over its summary
    fields as-is (unlike batch_copy_all_documents.py, which resets them for a fresh copy).

    Returns (status, detail) where status is one of:
    'copied', 'would_copy', 'not_found_in_storage', 'copy_error'."""
    _, ext = os.path.splitext(doc["filename"])

    detail = {
        "sheet": pair.sheet,
        "row": pair.row_number,
        "source_file_id": str(doc["file_id"]),
        "filename": doc["filename"],
        "source_state_center_id": pair.source_state_center_id,
        "source_department_id": pair.source_department_id,
        "target_state_center_id": pair.target_state_center_id,
        "target_department_id": pair.target_department_id,
        "source_stored_path": doc["stored_path"],
        "source_summary_status": doc["summary_status"],
        "target_user_id": str(target_user_id),
    }

    # This GCS existence check always runs, dry-run or not -- otherwise --dry-run's
    # "not_found_in_storage" would always report 0 without ever actually checking.
    # Wrapped in asyncio.to_thread since google-cloud-storage's client is blocking/
    # synchronous -- without this, concurrent documents would just queue up behind
    # each other on the event loop instead of actually running in parallel.
    source_blob = bucket.blob(doc["stored_path"])
    exists = await asyncio.to_thread(source_blob.exists)
    if not exists:
        logger.warning("GCS_CHECK: NOT FOUND -- %s (file_id=%s) has no file at %s",
                        doc["filename"], doc["file_id"], doc["stored_path"])
        return "not_found_in_storage", detail
    logger.info("GCS_CHECK: FOUND -- %s (file_id=%s) at %s", doc["filename"], doc["file_id"], doc["stored_path"])

    # Computed here (not just on the real-copy path) so dry-run output shows exactly
    # where a real run would place the object -- this is pure path math, no GCS/DB
    # side effects, so it's safe to compute unconditionally.
    new_uuid_filename = f"{uuid.uuid4()}{ext}"
    new_stored_path = build_blob_name(gcs_prefix, pair.target_state_center_id, pair.target_department_id, new_uuid_filename)
    detail["new_stored_path"] = new_stored_path

    if dry_run:
        print(f"Dry run: would copy {doc['filename']} ({pair.source_state_center_id}/{pair.source_department_id or '_root_'} "
              f"-> {pair.target_state_center_id}/{pair.target_department_id or '_root_'})")
        return "would_copy", detail

    try:
        file_bytes = await asyncio.to_thread(source_blob.download_as_bytes)
        new_blob = bucket.blob(new_stored_path)
        await asyncio.to_thread(new_blob.upload_from_string, file_bytes, content_type="application/pdf")
        new_size = len(file_bytes)

        new_file_id = uuid.uuid4()
        await insert_document(
            conn,
            file_id=new_file_id,
            state_center_id=pair.target_state_center_id,
            department_id=pair.target_department_id,
            uploader_id=target_user_id,
            filename=doc["filename"],
            document_type=doc["document_type"],
            document_name=doc["document_name"],
            stored_path=new_stored_path,
            file_size_bytes=new_size,
            # Summary IS carried over as-is -- this is the whole point of this script vs.
            # batch_copy_all_documents.py, which always resets it for a fresh copy.
            summary_status=doc["summary_status"],
            summary_text=doc["summary_text"],
            summary_error=doc["summary_error"],
            last_summary_request_id=doc["last_summary_request_id"],
        )
        logger.info("COPIED: %s -> %s (summary_status=%s)", doc["filename"], new_stored_path, doc["summary_status"])
        detail["new_file_id"] = str(new_file_id)
        return "copied", detail
    except Exception as exc:
        # Log and move on to the next document instead of aborting the whole run --
        # one bad file should not stop the rest from being processed.
        logger.exception("COPY_FAILED: %s (file_id=%s): %s", doc["filename"], doc["file_id"], exc)
        detail["error"] = str(exc)
        return "copy_error", detail


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

async def run_copy_between_scopes(
    pool: asyncpg.Pool,
    bucket,
    gcs_prefix: str,
    pairs: list,
    target_user_id: uuid.UUID,
    dry_run: bool = False,
    concurrency: int = 10,
):
    """For each scope-pair row, fetch every document in the source scope (any uploader) and
    copy it into the row's target scope, owned by target_user_id. Processes up to
    `concurrency` documents at once across all rows combined."""
    work_items = []  # list of (doc, pair)
    outcomes = []
    empty_scope_pairs = 0
    already_in_target = 0
    for pair in pairs:
        docs = await get_documents_in_scope(pool, pair.source_state_center_id, pair.source_department_id)
        docs = dedupe_documents_by_filename(docs)
        logger.info(
            "SCOPE (row %s, %s): %s -> %s: found %s document(s) after dedup",
            pair.row_number, pair.sheet, pair.source_state_center_id, pair.target_state_center_id, len(docs),
        )
        if not docs:
            # The row's source scope has no documents at all -- record it as its own
            # outcome row (not silently dropped from work_items) so it's visible in the
            # CSV instead of just vanishing from the totals.
            empty_scope_pairs += 1
            logger.warning(
                "SCOPE (row %s, %s): no documents found for source scope %s/%s -> skipping row",
                pair.row_number, pair.sheet, pair.source_state_center_id, pair.source_department_id or "_root_",
            )
            outcomes.append({
                "status": "no_documents_in_scope",
                "sheet": pair.sheet,
                "row": pair.row_number,
                "source_state_center_id": pair.source_state_center_id,
                "source_department_id": pair.source_department_id,
                "target_state_center_id": pair.target_state_center_id,
                "target_department_id": pair.target_department_id,
                "target_user_id": str(target_user_id),
                "error": "no documents found for source scope",
            })
            continue

        # Skip any source document whose filename already exists in the target scope FOR THIS
        # TARGET USER -- otherwise a re-run of this script (or two rows landing on the same
        # target scope) would keep piling up duplicate copies for that user.
        existing_target_filenames = await get_existing_filenames_in_scope(
            pool, pair.target_state_center_id, pair.target_department_id, target_user_id
        )
        for doc in docs:
            if doc["filename"] in existing_target_filenames:
                already_in_target += 1
                logger.info(
                    "SKIP (row %s, %s): %s already exists in target scope %s/%s -- not copying",
                    pair.row_number, pair.sheet, doc["filename"],
                    pair.target_state_center_id, pair.target_department_id or "_root_",
                )
                outcomes.append({
                    "status": "skipped_already_in_target",
                    "sheet": pair.sheet,
                    "row": pair.row_number,
                    "source_file_id": str(doc["file_id"]),
                    "filename": doc["filename"],
                    "source_state_center_id": pair.source_state_center_id,
                    "source_department_id": pair.source_department_id,
                    "target_state_center_id": pair.target_state_center_id,
                    "target_department_id": pair.target_department_id,
                    "source_stored_path": doc["stored_path"],
                    "source_summary_status": doc["summary_status"],
                    "target_user_id": str(target_user_id),
                    "error": "filename already exists in target scope",
                })
                continue
            work_items.append((doc, pair))

    total = len(work_items)
    logger.info(
        "TOTAL: %s document(s) found across %s scope-pair row(s) (%s row(s) had no documents, "
        "%s already present in their target scope)",
        total, len(pairs), empty_scope_pairs, already_in_target,
    )

    counts = {"copied": 0, "would_copy": 0, "not_found_in_storage": 0, "copy_error": 0}
    counts_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    progress = {"done": 0}

    async def process_one(doc, pair):
        async with semaphore:
            status, detail = await _copy_one_document(pool, bucket, gcs_prefix, doc, pair, target_user_id, dry_run)
        async with counts_lock:
            counts[status] = counts.get(status, 0) + 1
            progress["done"] += 1
            if progress["done"] % 100 == 0:
                logger.info("PROGRESS: %s/%s documents processed", progress["done"], total)
            outcomes.append({"status": status, **detail})

    await asyncio.gather(*(process_one(doc, pair) for doc, pair in work_items))

    copied = counts["copied"]
    not_found_in_storage = counts["not_found_in_storage"]
    copy_errors = counts["copy_error"]
    found_in_gcs = total - not_found_in_storage

    action = "Would copy" if dry_run else "Copy completed."
    logger.info(
        "%s db_found=%s found_in_gcs=%s would_copy_or_copied=%s not_found_in_storage=%s copy_errors=%s",
        action, total, found_in_gcs, (found_in_gcs if dry_run else copied), not_found_in_storage, copy_errors,
    )

    summary = {
        "target_user_id": str(target_user_id),
        "scope_pairs": len(pairs),
        "scope_pairs_with_no_documents": empty_scope_pairs,
        "already_in_target": already_in_target,
        "dry_run": dry_run,
        "documents_found": total,
        "found_in_gcs": found_in_gcs,
        "copied": copied,
        "would_copy": counts["would_copy"],
        "not_found_in_storage": not_found_in_storage,
        "copy_errors": copy_errors,
    }

    write_outcome_csv(outcomes)
    logger.info("Outcome CSV written to: %s", OUTCOME_CSV_FILE)

    return summary


OUTCOME_CSV_COLUMNS = [
    "status", "sheet", "row", "source_file_id", "filename",
    "source_state_center_id", "source_department_id",
    "target_state_center_id", "target_department_id",
    "source_stored_path", "source_summary_status", "target_user_id",
    "new_stored_path", "new_file_id", "error",
]


def write_outcome_csv(outcomes: list) -> None:
    """One row per source document found (dry-run or --execute). Columns are the union of
    every possible per-document detail field across all four outcome statuses."""
    with open(OUTCOME_CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTCOME_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in outcomes:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in OUTCOME_CSV_COLUMNS})


def write_skipped_rows_note(skipped: list) -> None:
    if not skipped:
        return
    for s in skipped:
        logger.warning("SKIPPED ROW: sheet=%s row=%s reason=%s", s.sheet, s.row_number, s.reason)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def async_main():
    pairs, skipped = read_scope_pairs(INPUT_PATH, SHEET)
    write_skipped_rows_note(skipped)

    # A pool (not a single connection) so documents can be processed concurrently --
    # each concurrent task acquires its own connection from the pool.
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=max(CONCURRENCY, 2))
    try:
        client = storage.Client.from_service_account_json(GCS_CREDENTIALS)
        bucket = client.bucket(GCS_BUCKET)
        return await run_copy_between_scopes(
            pool, bucket, GCS_PREFIX,
            pairs=pairs,
            target_user_id=TARGET_USER_ID,
            dry_run=DRY_RUN,
            concurrency=CONCURRENCY,
        )
    finally:
        await pool.close()


def main():
    log_file = configure_logging()
    try:
        result = asyncio.run(async_main())
        logger.info("=" * 100)
        logger.info("RUN SUMMARY")
        logger.info("=" * 100)
        for key, value in result.items():
            logger.info("%s: %s", key, value)
        logger.info("Outcome CSV: %s", OUTCOME_CSV_FILE)
        logger.info("Detailed logs written to: %s", log_file)
        logger.info("=" * 100)
        print(result)
    except Exception as exc:
        logger.exception("Migration failed")
        print(f"Migration failed: {exc}")
        raise


if __name__ == "__main__":
    main()

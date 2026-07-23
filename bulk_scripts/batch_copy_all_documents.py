#!/usr/bin/env python3
"""Copy EVERY document in the documents table to one target user.

No CSV, no scope, no source user -- this script only acts on the documents
table directly: find every document row (excluding ones already owned by the
target), and for each one that has a real file in GCS, copy that file to a
new GCS object and insert a new row owned by the target user.

Deduplicated by (state_center_id, department_id, filename): if the same
document was uploaded by multiple different users under the same state/
department, only the most recently created copy is kept for copying (an
empty/NULL department_id is treated as its own consistent group, not skipped).

Fully self-contained: does NOT import anything from this repo's src/ (no app
config, no ORM models). Talks to Postgres directly via asyncpg and to GCS
directly via google-cloud-storage.

DATABASE_URL, GCS bucket/credentials/prefix are all read from the repo's
.env file (see ENV_FILE below) -- nothing is passed on the command line.
--user-id (mandatory) and --batch-size (mandatory, default 10) are CLI args.

Every copy gets a brand-new database row and a brand-new GCS object -- never
an update, never an overwrite. The DB row keeps the ORIGINAL filename as-is;
the GCS object always gets a fresh UUID-based path, so re-running this script
does not collide with, skip, or overwrite anything copied by a previous run.

DRY RUN BY DEFAULT: without --execute, nothing is written -- only a plan is
computed and a JSON preview is written. Pass --execute to perform real copies.

Usage:
    python batch_copy_all_documents.py --user-id <UUID>                       # dry run (default)
    python batch_copy_all_documents.py --user-id <UUID> --execute             # real run
    python batch_copy_all_documents.py --user-id <UUID> --execute --batch-size 20
"""

import argparse
import asyncio
import json
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
logger = logging.getLogger("copy_all_documents")

# --------------------------------------------------------------------------
# CLI args -- --user-id and --batch-size are mandatory; --execute opts into a
# real run (default is dry-run).
# --------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy every document in the documents table to one target user."
    )
    parser.add_argument("--user-id", required=True, type=uuid.UUID,
                        help="Target user UUID (owner of every copied document). Mandatory.")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="How many documents to process concurrently (default: 10).")
    parser.add_argument("--execute", action="store_true",
                        help="Perform real copies. Without this flag, the script only does a dry run "
                             "(no DB writes, no GCS uploads) and writes a JSON preview.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum documents to process (default: unlimited -- the whole table).")
    return parser.parse_args()


_ARGS = parse_args()

# --------------------------------------------------------------------------
# Script-level configuration derived from CLI args
# --------------------------------------------------------------------------

TARGET_USER_ID = _ARGS.user_id
CONCURRENCY = _ARGS.batch_size
LIMIT: Optional[int] = _ARGS.limit
DRY_RUN = not _ARGS.execute

ENV_FILE = SCRIPT_DIR / ".." / ".env"

LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOGS_DIR / f"copy_all_documents_{RUN_TIMESTAMP}.log"
DRY_RUN_OUTPUT_JSON = LOGS_DIR / f"copy_all_documents_{RUN_TIMESTAMP}_dry_run.json"


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
# DB helpers (raw asyncpg -- no ORM)
# --------------------------------------------------------------------------

async def get_all_documents(conn, exclude_uploader_id, limit: Optional[int]):
    """Every row in the documents table, excluding documents already owned by the
    target (can't copy something to its own owner), deduplicated by
    (state_center_id, department_id, filename) -- see dedupe_documents_by_scope_and_filename.
    limit=None means no cap on the deduplicated result -- the whole table."""
    rows = await conn.fetch(
        "SELECT * FROM documents WHERE uploader_id IS DISTINCT FROM $1 ORDER BY created_at DESC",
        exclude_uploader_id,
    )
    deduped = dedupe_documents_by_scope_and_filename(rows)
    return deduped[:limit] if limit else deduped


def dedupe_documents_by_scope_and_filename(rows):
    """The same document (same filename) can be uploaded by different users under the
    same state_center_id/department_id -- only one copy per (state_center_id,
    department_id, filename) should ever be copied to the target user, not one per
    uploader. department_id is frequently empty/NULL, so it's normalized to a
    consistent placeholder for the dedup key (mirrors build_blob_name's "_root_"
    treatment of a missing department).

    `rows` must already be ordered by created_at DESC (as get_all_documents does) --
    dict.setdefault then keeps the FIRST row seen per key, i.e. the most recently
    created one, and silently drops the rest."""
    seen: dict = {}
    for row in rows:
        key = (row["state_center_id"], row["department_id"] or "_root_", row["filename"])
        seen.setdefault(key, row)
    deduped = list(seen.values())
    dropped = len(rows) - len(deduped)
    if dropped:
        logger.info(
            "DEDUPE: %s duplicate document(s) skipped (same state_center_id/department_id/filename, "
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

async def _copy_one_document(conn, bucket, gcs_prefix, doc, target_user_id, dry_run):
    """Always copies if a real file is found in GCS -- never skips as a "duplicate";
    the GCS object always gets a fresh UUID-based path, so nothing is ever overwritten
    in storage even though the DB row keeps the original filename.

    Returns (status, detail) where status is one of:
    'copied', 'would_copy', 'not_found_in_storage', 'copy_error', and detail is a dict
    describing the source document plus (when applicable) where it was/would be copied
    to -- used to build the dry-run JSON output and the final run report."""
    state_center_id = doc["state_center_id"]
    department_id = doc["department_id"]
    _, ext = os.path.splitext(doc["filename"])

    detail = {
        "source_file_id": str(doc["file_id"]),
        "filename": doc["filename"],
        "state_center_id": state_center_id,
        "department_id": department_id,
        "source_stored_path": doc["stored_path"],
        "target_user_id": str(target_user_id),
    }

    # This GCS existence check always runs, dry-run or not -- otherwise --dry-run's
    # "not_found_in_storage" would always report 0 without ever actually checking,
    # giving a false preview of what a real run would find.
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
    new_stored_path = build_blob_name(gcs_prefix, state_center_id, department_id, new_uuid_filename)
    detail["new_stored_path"] = new_stored_path

    if dry_run:
        print(f"Dry run: would copy {doc['filename']}")
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
            state_center_id=state_center_id,
            department_id=department_id,
            uploader_id=target_user_id,
            filename=doc["filename"],
            document_type=doc["document_type"],
            document_name=doc["document_name"],
            stored_path=new_stored_path,
            file_size_bytes=new_size,
            # Summary is not carried over -- the new owner's copy starts fresh,
            # not pre-filled with the source document's (possibly stale) summary.
            summary_status="NOT_STARTED",
            summary_text=None,
            summary_error=None,
            last_summary_request_id=None,
        )
        logger.info("COPIED: %s -> %s", doc["filename"], new_stored_path)
        detail["new_file_id"] = str(new_file_id)
        return "copied", detail
    except Exception as exc:
        # Log and move on to the next document instead of aborting the whole run --
        # one bad file should not stop the rest from being processed. Tracked
        # separately from not_found_in_storage: this file WAS found in GCS,
        # something else went wrong copying it (network error, DB error, etc).
        logger.exception("COPY_FAILED: %s (file_id=%s): %s", doc["filename"], doc["file_id"], exc)
        detail["error"] = str(exc)
        return "copy_error", detail


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

async def run_copy_all(
    pool: asyncpg.Pool,
    bucket,
    gcs_prefix: str,
    target_user_id: uuid.UUID,
    dry_run: bool = False,
    limit: Optional[int] = None,
    concurrency: int = 10,
):
    """Copy every document in the documents table to one target user. Each document
    keeps its own original state_center_id/department_id -- there's no scope to
    specify, since every row already carries its own.

    Processes up to `concurrency` documents at once (the pool lets each concurrent
    task grab its own DB connection; GCS calls run in a thread via asyncio.to_thread)
    -- sequential, one-at-a-time processing would be far too slow at real table sizes."""
    docs = await get_all_documents(pool, target_user_id, limit)
    total = len(docs)
    logger.info("ALL_DOCS: found %s total document row(s) in the documents table (excluding target's own)", total)

    counts = {"copied": 0, "would_copy": 0, "not_found_in_storage": 0, "copy_error": 0}
    counts_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    progress = {"done": 0}
    outcomes = []

    async def process_one(doc):
        async with semaphore:
            status, detail = await _copy_one_document(pool, bucket, gcs_prefix, doc, target_user_id, dry_run)
        async with counts_lock:
            counts[status] = counts.get(status, 0) + 1
            progress["done"] += 1
            if progress["done"] % 100 == 0:
                logger.info("PROGRESS: %s/%s documents processed", progress["done"], total)
            outcomes.append({"status": status, **detail})

    await asyncio.gather(*(process_one(doc) for doc in docs))

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
        "dry_run": dry_run,
        "documents_found": total,
        "found_in_gcs": found_in_gcs,
        "copied": copied,
        "would_copy": counts["would_copy"],
        "not_found_in_storage": not_found_in_storage,
        "copy_errors": copy_errors,
    }

    if dry_run:
        with open(DRY_RUN_OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "documents": outcomes}, f, indent=2, ensure_ascii=False)
        logger.info("Dry-run JSON output written to: %s", DRY_RUN_OUTPUT_JSON)

    return summary


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

async def async_main():
    # A pool (not a single connection) so documents can be processed concurrently --
    # each concurrent task acquires its own connection from the pool. asyncpg.Pool
    # exposes the same .fetch/.fetchrow/.execute methods as a plain Connection, so
    # every DB helper function above works unchanged either way.
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=max(CONCURRENCY, 2))
    try:
        client = storage.Client.from_service_account_json(GCS_CREDENTIALS)
        bucket = client.bucket(GCS_BUCKET)
        return await run_copy_all(
            pool, bucket, GCS_PREFIX,
            target_user_id=TARGET_USER_ID,
            dry_run=DRY_RUN,
            limit=LIMIT,
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
        if DRY_RUN:
            logger.info("Dry-run JSON output: %s", DRY_RUN_OUTPUT_JSON)
        logger.info("Detailed logs written to: %s", log_file)
        logger.info("=" * 100)
        print(result)
    except Exception as exc:
        logger.exception("Migration failed")
        print(f"Migration failed: {exc}")
        raise


if __name__ == "__main__":
    main()

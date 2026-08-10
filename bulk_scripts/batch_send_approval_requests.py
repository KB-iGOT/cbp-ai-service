"""
Standalone batch script: submit bulk approval requests for CBP plans, driven by an
input file (.xlsx/.xlsm or .csv) where each row directly identifies one designation's CBP
plan to submit -- role_mapping_id and recommendation_id are read straight from the file
(no more state/department scope lookup). mdo_id is read too but is OPTIONAL: if the column
is present and the cell is non-blank it's used as-is, otherwise the row's mdo_id is stored
as NULL -- a missing mdo_id never skips or fails a row. ONE approval request is created per
row, containing exactly ONE approval_request_item (one CBP plan per designation).

This script is FULLY SELF-CONTAINED. It does not import anything from `src/` and does
not call the HTTP API. All logic (role-mapping/CBP-plan lookup, approval request +
approval request item persistence, MDO email notification) is re-implemented here
directly against the database and the iGOT user-search / notification services, using
the same logic as:
    - src/api/v1/approval_requests.py  (send_for_approval)
    - src/crud/approval_request.py     (create_approval_request)
    - src/models/approval_request.py   (ApprovalRequest, ApprovalRequestItem)
    - src/services/notification_service.py (send_cbp_approval_email)
    - src/services/user_search_service.py  (search_users)

Flow per Excel row (role_mapping_id, recommendation_id mandatory; mdo_id optional):
    1. Fetch the role_mapping by id (must belong to --user-id).
    2. Fetch the CBP plan matching role_mapping_id AND recommendation_id AND user_id.
       If no matching CBP plan exists, the row is skipped (logged, not fatal to the batch).
    3. Build ONE approval request containing exactly one approval_request_item (this
       designation's CBP plan), named "CBP Plan for <designation_name>".
    4. An MDO approval email notification is attempted for the created request (same as
       the live API's background task), using the row's mdo_id if one was given -- if
       mdo_id is blank/absent, no MDO admin can be looked up so the email is skipped
       (logged), same as when no matching MDO admin is found for a real mdo_id.
    5. No idempotency/dedup check is performed -- every run creates fresh approval
       requests for every eligible row in the input (matches "always submit").
    6. Any failure for a given row is caught, logged, and the run continues to the next
       row -- a single row's failure never aborts the batch.

Concurrency: up to --batch-size (default 10) rows are processed concurrently via an
asyncio.Semaphore. Retry: transient DB/HTTP failures are retried with exponential
backoff (see `with_retry`).

Dry run is the DEFAULT: a zero-cost plan preview. No database writes and no email
notification is made -- rows with both a role_mapping and a matching CBP plan are
reported as WOULD_CREATE (no approval request is actually built or persisted). Pass
--execute to perform real writes and send real notifications.

All environment variables this script depends on are mandatory -- a clear error is
raised and the script exits if any is missing, no silent defaults.

Mandatory CLI args: --excel, --user-id. --batch-size defaults to 10. --execute opts
into a real run (dry-run is the default).

Mandatory input columns: role_mapping_id, recommendation_id. If either is missing from
the file entirely, the script raises a fatal error and exits immediately. mdo_id is
optional -- if the column is absent, or present but blank on a given row, that row's
mdo_id is simply stored as NULL; it never causes a row to be skipped or the script to
exit. The remaining columns (state_center_id, department_id, org_type, state_center_name,
department_name, designation) are read if present and echoed into the outcome CSV, but
are not required for processing.

Outcome is written as a CSV (not JSON) alongside the input file: input columns +
approval_request_id, request_name, designation_count, approval_request_item_ids,
status, error.

Logging: all logs go to a dedicated log file under bulk_scripts/logs/ (timestamped
per run), in addition to the console. A run summary is printed and logged at the end.

Usage:
    python bulk_scripts/batch_send_approval_requests.py --excel <path> --user-id <uuid>
    python bulk_scripts/batch_send_approval_requests.py --excel <path> --user-id <uuid> --batch-size 20 --execute
"""

import argparse
import asyncio
import contextlib
import csv
import enum
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import openpyxl
from sqlalchemy import Column, DateTime, Enum as SAEnum, Integer, String, and_, select
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# --------------------------------------------------------------------------------------
# CLI args -- --excel and --user-id are mandatory; --batch-size defaults to 10;
# dry-run is the DEFAULT; pass --execute to opt into real database writes + emails.
# --------------------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Submit bulk approval requests for CBP plans listed in an input file "
                     "(.xlsx/.xlsm or .csv; one row = one role_mapping/recommendation = one approval request)."
    )
    parser.add_argument("--excel", required=True,
                        help="Path to the source input file: .xlsx/.xlsm or .csv. Must contain "
                             "'role_mapping_id' and 'recommendation_id' columns. An 'mdo_id' column is "
                             "optional -- a missing/blank mdo_id is stored as NULL. Mandatory.")
    parser.add_argument("--user-id", required=True, type=uuid.UUID,
                        help="Owner user UUID for the role_mappings/recommendations/CBP plans being submitted. Mandatory.")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="How many rows to process concurrently (default: 10).")
    parser.add_argument("--execute", action="store_true",
                        help="Perform real database writes and send real email notifications. "
                             "Without this flag, the script runs in dry-run mode (default): no "
                             "writes/emails happen, output is captured to the outcome CSV instead.")
    return parser.parse_args()


_ARGS = parse_args()

EXCEL_FILE = _ARGS.excel
USER_ID = _ARGS.user_id
MAX_CONCURRENCY = _ARGS.batch_size
RETRY_ATTEMPTS = 3
RETRY_INITIAL_DELAY_SECONDS = 2.0
RETRY_EXP_BASE = 2.0

# Dry-run is the DEFAULT; --execute opts into real writes. In dry-run, no rows are written
# to approval_requests / approval_request_items, and no email notification is sent.
DRY_RUN = not _ARGS.execute

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
RUN_TIMESTAMP = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"batch_send_approval_requests_{RUN_TIMESTAMP}.log")

# Outcome CSV is written alongside the input Excel file, so it sits next to the file the
# user is already tracking for this batch.
OUTCOME_CSV_FILE = os.path.join(
    os.path.dirname(os.path.abspath(EXCEL_FILE)),
    f"batch_send_approval_requests_{RUN_TIMESTAMP}.csv",
)


# --------------------------------------------------------------------------------------
# .env loader (no python-dotenv dependency; standalone)
# --------------------------------------------------------------------------------------

def load_env_file(path: str) -> None:
    """Minimal .env parser: KEY=VALUE per line, '#' comments, optional quotes."""
    if not os.path.exists(path):
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
    no silent defaults."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Set it in {ENV_FILE} or in the environment before running this script."
        )
    return value


DATABASE_URL = require_env("DATABASE_URL")
KB_BASE_URL = require_env("KB_BASE_URL")
KB_AUTH_TOKEN = require_env("KB_AUTH_TOKEN")
NOTIFICATION_BASE_URL = require_env("NOTIFICATION_BASE_URL")
MDO_PORTAL_URL = require_env("MDO_PORTAL_URL")
ENABLE_EMAIL_NOTIFICATION = require_env("ENABLE_EMAIL_NOTIFICATION").strip().lower() in ("1", "true", "yes")

EMAIL_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "templates", "emails", "cbplan_request_email.html"
)


# --------------------------------------------------------------------------------------
# Logging setup (console + dedicated file)
# --------------------------------------------------------------------------------------

logger = logging.getLogger("batch_send_approval_requests")
logger.setLevel(logging.INFO)
logger.propagate = False

_formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)


# --------------------------------------------------------------------------------------
# SQLAlchemy models (minimal standalone re-declaration matching src/models/*)
# --------------------------------------------------------------------------------------

Base = declarative_base()


class ApprovalStatus(str, enum.Enum):
    FAILED = "failed"
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RoleMapping(Base):
    __tablename__ = "role_mappings"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    org_type = Column(String(20), nullable=True)
    state_center_id = Column(String(32), nullable=False)
    department_id = Column(String(32), nullable=True)
    state_center_name = Column(String(255), nullable=True)
    department_name = Column(String(255), nullable=True)
    status = Column(String(50), nullable=True)
    designation_name = Column(String(255), nullable=True)
    wing_division_section = Column(String(255), nullable=True)
    role_responsibilities = Column(JSONB, nullable=True)
    activities = Column(JSONB, nullable=True)
    competencies = Column(JSONB, nullable=True)
    sort_order = Column(Integer, nullable=True)
    igot_designation_name = Column(String(255), nullable=True)
    igot_designation_id = Column(String(255), nullable=True)


class CBPPlan(Base):
    __tablename__ = "cbp_plans"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    role_mapping_id = Column(PG_UUID(as_uuid=True), nullable=False)
    recommended_course_id = Column(PG_UUID(as_uuid=True), nullable=True)
    selected_courses = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    request_name = Column(String(100), nullable=False)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    org_type = Column(String(20), nullable=True)
    state_center_id = Column(String(255), nullable=False)
    department_id = Column(String(255), nullable=True)
    state_center_name = Column(String(255), nullable=False)
    department_name = Column(String(255), nullable=True)
    mdo_id = Column(String(255), nullable=True)
    designation_count = Column(Integer, nullable=False, default=0)
    status = Column(SAEnum(ApprovalStatus, name="approval_status_enum", create_type=False), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class ApprovalRequestItem(Base):
    __tablename__ = "approval_request_items"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    approval_request_id = Column(PG_UUID(as_uuid=True), nullable=False)
    source_role_mapping_id = Column(PG_UUID(as_uuid=True), nullable=False)
    designation_name = Column(String(255), nullable=False)
    wing_division_section = Column(String(255), nullable=True)
    role_responsibilities = Column(JSONB, nullable=True)
    activities = Column(JSONB, nullable=True)
    competencies = Column(JSONB, nullable=True)
    sort_order = Column(Integer, nullable=True)
    igot_designation_name = Column(String(255), nullable=True)
    igot_designation_id = Column(String(255), nullable=True)
    cbp_plan_data = Column(JSON, nullable=True)
    status = Column(SAEnum(ApprovalStatus, name="approval_request_item_status_enum", create_type=False), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------------------------
# DB engine / session
# --------------------------------------------------------------------------------------

engine = create_async_engine(DATABASE_URL, pool_size=10, max_overflow=MAX_CONCURRENCY, pool_recycle=1800, echo=False)
SessionLocal = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=AsyncSession)


@contextlib.asynccontextmanager
async def get_session():
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            # If the connection itself already died (e.g. dropped under concurrent pool
            # pressure), rollback/close raise their own InterfaceError -- suppressed so the
            # ORIGINAL error is what propagates, instead of a confusing "cannot rollback:
            # connection is closed" replacing it and escaping process_row's exception handling.
            with contextlib.suppress(Exception):
                await session.rollback()
            raise
        finally:
            with contextlib.suppress(Exception):
                await session.close()


# --------------------------------------------------------------------------------------
# Retry helper
# --------------------------------------------------------------------------------------

async def with_retry(coro_fn, *args, description: str = "operation", **kwargs):
    """Calls coro_fn(*args, **kwargs) with exponential-backoff retry."""
    delay = RETRY_INITIAL_DELAY_SECONDS
    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt == RETRY_ATTEMPTS:
                break
            logger.warning(
                f"  [retry] {description} failed on attempt {attempt}/{RETRY_ATTEMPTS}: {e}. "
                f"Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)
            delay *= RETRY_EXP_BASE
    raise last_exc


# --------------------------------------------------------------------------------------
# Excel reading
# --------------------------------------------------------------------------------------

@dataclass
class ExcelRow:
    row_number: int
    state_center_id: str
    department_id: str
    org_type: str
    state_center_name: str
    department_name: str
    designation: str
    role_mapping_id: uuid.UUID
    recommendation_id: uuid.UUID
    mdo_id: str


@dataclass
class SkippedRow:
    """A data row dropped by read_excel_rows because role_mapping_id/recommendation_id
    was blank or not a valid UUID -- kept (with all its other input columns, same
    as ExcelRow) so it can still be reported as its own row in the outcome CSV and RUN
    SUMMARY, instead of only affecting a count in a log line. mdo_id is never a reason
    a row lands here -- it's optional."""
    row_number: int
    state_center_id: str
    department_id: str
    org_type: str
    state_center_name: str
    department_name: str
    designation: str
    raw_role_mapping_id: str
    raw_recommendation_id: str
    raw_mdo_id: str
    reason: str


def _iter_input_rows(path: str) -> Tuple[List[str], "list"]:
    """Reads the input file's header row + data rows into a uniform shape regardless of format:
    (lowercased headers, list of raw-value lists aligned to those headers). Supports .xlsx/.xlsm
    (via openpyxl) and .csv (via the stdlib csv module) -- both input formats share the exact same
    downstream row-processing logic in read_excel_rows below."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        workbook = openpyxl.load_workbook(path)
        worksheet = workbook.active
        headers = [str(c.value).strip().lower() if c.value is not None else "" for c in worksheet[1]]
        data_rows = [
            [cell.value for cell in row]
            for row in worksheet.iter_rows(min_row=2, values_only=False)
        ]
        return headers, data_rows

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            headers = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            return [], []
        data_rows = [list(raw) for raw in reader]
        return headers, data_rows


def read_excel_rows(path: str) -> Tuple[List[ExcelRow], List[SkippedRow]]:
    """
    Reads all columns from the input file (.xlsx/.xlsm or .csv). 'role_mapping_id' and
    'recommendation_id' are MANDATORY columns -- if either is missing from the file
    entirely, this raises SystemExit immediately (fatal, whole-script error). 'mdo_id' is
    OPTIONAL: if the column is absent, or present but blank on a row, that row's mdo_id is
    simply "" (stored as NULL later) -- it's never a reason to skip a row. The other
    columns (state_center_id, department_id, org_type, state_center_name, department_name,
    designation) are read if present (empty string otherwise) and are not required for
    processing -- they're only echoed into the outcome CSV.

    A row whose role_mapping_id/recommendation_id value is blank or not a valid UUID is
    skipped (logged as a warning), not fatal to the rest of the file.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Input file not found: {path}")

    header_list, data_rows = _iter_input_rows(path)
    headers = {h: i for i, h in enumerate(header_list) if h}

    role_mapping_id_col = headers.get("role_mapping_id")
    recommendation_id_col = headers.get("recommendation_id")
    mdo_id_col = headers.get("mdo_id")  # optional -- absent column or blank cell -> mdo_id=""

    missing = [name for name, col in [
        ("role_mapping_id", role_mapping_id_col),
        ("recommendation_id", recommendation_id_col),
    ] if col is None]
    if missing:
        raise SystemExit(
            f"[config] required column(s) missing from input file: {missing}. "
            f"Headers found: {list(headers.keys())}. Path: {path}"
        )

    optional_cols = {
        "state_center_id": headers.get("state_center_id"),
        "department_id": headers.get("department_id"),
        "org_type": headers.get("org_type"),
        "state_center_name": headers.get("state_center_name"),
        "department_name": headers.get("department_name"),
        "designation": headers.get("designation"),
    }

    def _optional_value(row, col_idx) -> str:
        if col_idx is None or col_idx >= len(row):
            return ""
        val = row[col_idx]
        return str(val).strip() if val not in (None, "") else ""

    rows: List[ExcelRow] = []
    skipped_rows: List[SkippedRow] = []
    for row_idx, row in enumerate(data_rows, start=2):
        if not row or all(v is None or str(v).strip() == "" for v in row):
            continue
        rm_raw = row[role_mapping_id_col] if role_mapping_id_col < len(row) else None
        rec_raw = row[recommendation_id_col] if recommendation_id_col < len(row) else None
        mdo_raw = row[mdo_id_col] if mdo_id_col < len(row) else None

        skipped_row_kwargs = dict(
            row_number=row_idx,
            state_center_id=_optional_value(row, optional_cols["state_center_id"]),
            department_id=_optional_value(row, optional_cols["department_id"]),
            org_type=_optional_value(row, optional_cols["org_type"]),
            state_center_name=_optional_value(row, optional_cols["state_center_name"]),
            department_name=_optional_value(row, optional_cols["department_name"]),
            designation=_optional_value(row, optional_cols["designation"]),
            raw_role_mapping_id=str(rm_raw) if rm_raw not in (None, "") else "",
            raw_recommendation_id=str(rec_raw) if rec_raw not in (None, "") else "",
            raw_mdo_id=str(mdo_raw) if mdo_raw not in (None, "") else "",
        )

        if rm_raw in (None, "") or rec_raw in (None, ""):
            reason = (
                f"missing role_mapping_id/recommendation_id "
                f"(role_mapping_id={rm_raw!r}, recommendation_id={rec_raw!r})"
            )
            logger.warning(f"Row {row_idx}: {reason} -> skipping row")
            skipped_rows.append(SkippedRow(**skipped_row_kwargs, reason=reason))
            continue

        try:
            rm_id = uuid.UUID(str(rm_raw).strip())
        except ValueError:
            reason = f"role_mapping_id {rm_raw!r} is not a valid UUID"
            logger.warning(f"Row {row_idx}: {reason} -> skipping row")
            skipped_rows.append(SkippedRow(**skipped_row_kwargs, reason=reason))
            continue

        try:
            rec_id = uuid.UUID(str(rec_raw).strip())
        except ValueError:
            reason = f"recommendation_id {rec_raw!r} is not a valid UUID"
            logger.warning(f"Row {row_idx}: {reason} -> skipping row")
            skipped_rows.append(SkippedRow(**skipped_row_kwargs, reason=reason))
            continue

        rows.append(
            ExcelRow(
                row_number=row_idx,
                state_center_id=_optional_value(row, optional_cols["state_center_id"]),
                department_id=_optional_value(row, optional_cols["department_id"]),
                org_type=_optional_value(row, optional_cols["org_type"]),
                state_center_name=_optional_value(row, optional_cols["state_center_name"]),
                department_name=_optional_value(row, optional_cols["department_name"]),
                designation=_optional_value(row, optional_cols["designation"]),
                role_mapping_id=rm_id,
                recommendation_id=rec_id,
                mdo_id=str(mdo_raw).strip() if mdo_raw not in (None, "") else "",
            )
        )

    logger.info(
        f"Read {len(rows)} valid data row(s) from input file "
        f"({len(skipped_rows)} skipped due to missing/invalid role_mapping_id/recommendation_id): {path}"
    )
    return rows, skipped_rows


# --------------------------------------------------------------------------------------
# Role mapping + CBP plan lookup
# --------------------------------------------------------------------------------------

async def fetch_role_mapping_by_id(db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[RoleMapping]:
    stmt = select(RoleMapping).where(RoleMapping.id == role_mapping_id, RoleMapping.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def fetch_cbp_plan(
    db: AsyncSession, role_mapping_id: uuid.UUID, recommendation_id: uuid.UUID, user_id: uuid.UUID
) -> Optional[CBPPlan]:
    """Looks up the CBP plan matching role_mapping_id AND recommendation_id (recommended_course_id)
    AND user_id together -- both ids come straight from the Excel row, so this is the most
    precise match available."""
    stmt = select(CBPPlan).where(
        and_(
            CBPPlan.role_mapping_id == role_mapping_id,
            CBPPlan.recommended_course_id == recommendation_id,
            CBPPlan.user_id == user_id,
        )
    )
    result = await db.execute(stmt)
    return result.scalars().first()


# --------------------------------------------------------------------------------------
# MDO admin lookup + email notification (mirrors user_search_service + notification_service)
# --------------------------------------------------------------------------------------

async def search_mdo_admin_users(mdo_id: str) -> List[Dict[str, Any]]:
    """Mirrors UserSearchService.search_users, called with the same filter shape as
    NotificationService.send_cbp_approval_email uses to resolve a single MDO admin's email."""
    body = {
        "request": {
            "filters": {
                "status": 1,
                "organisations.roles": ["MDO_LEADER", "MDO_ADMIN"],
                "userId": mdo_id,
            },
            "fields": ["firstName", "lastName", "id", "rootOrgId", "organisations", "roles", "profileDetails"],
        }
    }
    url = f"{KB_BASE_URL}/api/private/user/v1/search"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json=body, headers={"Content-Type": "application/json", "Authorization": KB_AUTH_TOKEN})
        response.raise_for_status()
        data = response.json()
        return data.get("result", {}).get("response", {}).get("content", [])


_EMAIL_TEMPLATE_CACHE: Optional[str] = None


def _load_email_template() -> str:
    global _EMAIL_TEMPLATE_CACHE
    if _EMAIL_TEMPLATE_CACHE is None:
        with open(EMAIL_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            _EMAIL_TEMPLATE_CACHE = f.read()
    return _EMAIL_TEMPLATE_CACHE


async def send_cbp_approval_email(mdo_id: str, request_name: str, requested_by: str, request_id: str, label: str) -> None:
    """Mirrors NotificationService.send_cbp_approval_email. Never raises -- a failed/absent
    notification is logged and does not affect the row's SUCCEEDED outcome, matching the
    live API where this runs as a fire-and-forget background task."""
    if not ENABLE_EMAIL_NOTIFICATION:
        logger.info(f"      {label} -> email notifications disabled, skipping CBP approval email")
        return

    if not mdo_id:
        logger.info(f"      {label} -> no mdo_id for this row, skipping CBP approval email")
        return

    try:
        users = await with_retry(search_mdo_admin_users, mdo_id, description="search_mdo_admin_users")
    except Exception as e:
        logger.warning(f"      {label} -> failed to search MDO admin users for mdo_id={mdo_id}: {e}")
        return

    if not users:
        logger.warning(f"      {label} -> no MDO admin found for mdo_id={mdo_id}, skipping CBP approval email")
        return

    mdo_emails = []
    for user in users:
        profile = user.get("profileDetails") or {}
        personal = profile.get("personalDetails") or {}
        email = personal.get("primaryEmail")
        if email:
            mdo_emails.append(email)

    if not mdo_emails:
        logger.warning(f"      {label} -> no email found for MDO admin mdo_id={mdo_id}, skipping CBP approval email")
        return

    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    mdo_portal_url = f"{MDO_PORTAL_URL}/app/home/ai-cbp-requests/acbp-list/review-request/{request_id}?source=mdo"
    submitted_on = datetime.now(ist).strftime("%d %b %Y, %I:%M %p IST")
    template_data = _load_email_template()

    params = {
        "requestName": request_name,
        "requestedBy": requested_by,
        "submittedOn": submitted_on,
        "requestId": request_id,
        "approvalLink": mdo_portal_url,
    }

    payload = {
        "request": {
            "notifications": [
                {
                    "type": "email",
                    "priority": 1,
                    "ids": mdo_emails,
                    "bccIds": [],
                    "action": {
                        "type": "email",
                        "category": "email",
                        "createdBy": {"id": mdo_id, "type": "user"},
                        "template": {
                            "data": template_data,
                            "id": "cbp-plan-approval-request",
                            "params": params,
                            "type": "email",
                            "config": {
                                "subject": f"New Capacity Building Plan for {request_name} Submitted for Your Approval",
                                "sender": "",
                            },
                        },
                    },
                }
            ]
        }
    }

    try:
        url = f"{NOTIFICATION_BASE_URL}/v2/notification/send"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await with_retry(client.post, url, json=payload, headers={"Content-Type": "application/json"}, description="send_notification")
            response.raise_for_status()
        logger.info(f"      {label} -> CBP approval email sent to {len(mdo_emails)} recipient(s)")
    except Exception as e:
        logger.warning(f"      {label} -> failed to send CBP approval email: {e}")


# --------------------------------------------------------------------------------------
# Approval request creation (mirrors crud_approval_request.create_approval_request)
# --------------------------------------------------------------------------------------

REQUEST_NAME_MAX_LEN = 100


def build_request_name(designation_name: Optional[str], igot_designation_name: Optional[str] = None) -> str:
    """Request name is 'AI CBP for <designation>', preferring igot_designation_name over
    designation_name when both are present. Truncated to REQUEST_NAME_MAX_LEN (100) characters
    total, with a trailing '...' marker if it had to be cut."""
    designation = igot_designation_name or designation_name or "Unknown Designation"
    name = f"AI CBP for {designation}"
    if len(name) <= REQUEST_NAME_MAX_LEN:
        return name
    return name[:REQUEST_NAME_MAX_LEN - 3].rstrip() + "..."


def build_approval_request_item(role_mapping: RoleMapping, plan: CBPPlan) -> Dict[str, Any]:
    """Builds the single item snapshot dict for this row's role_mapping + CBP plan
    (mirrors the item construction loop in send_for_approval, but always exactly one
    item per approval request now -- one CBP plan per designation)."""
    return {
        "id": uuid.uuid4(),
        "source_role_mapping_id": role_mapping.id,
        "designation_name": role_mapping.designation_name,
        "wing_division_section": role_mapping.wing_division_section,
        "role_responsibilities": role_mapping.role_responsibilities,
        "activities": role_mapping.activities,
        "competencies": role_mapping.competencies,
        "igot_designation_name": role_mapping.igot_designation_name,
        "igot_designation_id": role_mapping.igot_designation_id,
        "cbp_plan_data": [
            {
                "id": str(plan.id),
                "user_id": str(plan.user_id),
                "role_mapping_id": str(plan.role_mapping_id),
                "recommended_course_id": str(plan.recommended_course_id) if plan.recommended_course_id else None,
                "selected_courses": plan.selected_courses or [],
                "created_at": plan.created_at.isoformat() if plan.created_at else None,
                "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
            }
        ],
        "sort_order": role_mapping.sort_order,
    }


async def create_approval_request_with_item(
    request_name: str,
    state_center_id: str,
    department_id: str,
    state_center_name: Optional[str],
    department_name: Optional[str],
    org_type: Optional[str],
    mdo_id: str,
    item: Dict[str, Any],
) -> ApprovalRequest:
    request_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with get_session() as db:
        approval_request = ApprovalRequest(
            id=request_id,
            request_name=request_name,
            user_id=USER_ID,
            state_center_id=state_center_id,
            department_id=department_id,
            state_center_name=state_center_name,
            department_name=department_name,
            org_type=org_type,
            mdo_id=mdo_id or None,  # blank mdo_id -> NULL, matching the now-nullable DB column
            designation_count=1,
            status=ApprovalStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        db.add(approval_request)

        db.add(ApprovalRequestItem(
            id=item["id"],
            approval_request_id=request_id,
            source_role_mapping_id=item["source_role_mapping_id"],
            designation_name=item["designation_name"],
            wing_division_section=item["wing_division_section"],
            role_responsibilities=item["role_responsibilities"],
            activities=item["activities"],
            competencies=item["competencies"],
            igot_designation_name=item["igot_designation_name"],
            igot_designation_id=item["igot_designation_id"],
            cbp_plan_data=item["cbp_plan_data"],
            sort_order=item["sort_order"],
            status=ApprovalStatus.PENDING,
            created_at=now,
        ))

        await db.commit()
        await db.refresh(approval_request)
        return approval_request


# --------------------------------------------------------------------------------------
# Per-row unit of work
# --------------------------------------------------------------------------------------

class RowResult(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    SKIPPED_ROLE_MAPPING_NOT_FOUND = "SKIPPED_ROLE_MAPPING_NOT_FOUND"
    SKIPPED_NO_CBP_PLAN = "SKIPPED_NO_CBP_PLAN"
    FAILED = "FAILED"
    WOULD_CREATE = "WOULD_CREATE"  # dry-run only: role_mapping + CBP plan both exist and an
    # approval request WOULD be created, but no DB write / email was actually performed.


@dataclass
class RowOutcome:
    excel_row: ExcelRow
    result: RowResult
    approval_request_id: Optional[uuid.UUID] = None
    request_name: Optional[str] = None
    designation_count: int = 0
    approval_request_item_ids: Optional[List[uuid.UUID]] = None
    error: Optional[str] = None


class ProgressTracker:
    """Assigns each row a stable [index/total] label as it starts, mirroring the pattern
    used in batch_generate_and_save_cbp_plan.py."""

    def __init__(self, total: int):
        self.total = total
        self._count = 0

    def next_index(self) -> int:
        self._count += 1
        return self._count


async def process_row(row: ExcelRow, semaphore: asyncio.Semaphore, progress: "ProgressTracker") -> RowOutcome:
    async with semaphore:
        index = progress.next_index()
        label = f"[{index}/{progress.total}] role_mapping={row.role_mapping_id} recommendation={row.recommendation_id} mdo={row.mdo_id} (excel row {row.row_number})"
        logger.info(f"START {label}")

        try:
            async with get_session() as db:
                role_mapping = await with_retry(
                    fetch_role_mapping_by_id, db, row.role_mapping_id, USER_ID,
                    description=f"fetch_role_mapping_by_id(row {row.row_number})",
                )

            if not role_mapping:
                error_message = f"role_mapping {row.role_mapping_id} not found for user {USER_ID}"
                logger.warning(f"SKIP  {label} -> {error_message}")
                return RowOutcome(excel_row=row, result=RowResult.SKIPPED_ROLE_MAPPING_NOT_FOUND, error=error_message)

            async with get_session() as db:
                plan = await with_retry(
                    fetch_cbp_plan, db, row.role_mapping_id, row.recommendation_id, USER_ID,
                    description=f"fetch_cbp_plan(row {row.row_number})",
                )

            if not plan:
                error_message = (
                    f"no CBP plan found for role_mapping_id={row.role_mapping_id}, "
                    f"recommendation_id={row.recommendation_id}, user_id={USER_ID}"
                )
                logger.warning(f"SKIP  {label} -> {error_message}")
                return RowOutcome(excel_row=row, result=RowResult.SKIPPED_NO_CBP_PLAN, error=error_message)

            request_name = build_request_name(role_mapping.designation_name, role_mapping.igot_designation_name)

            if DRY_RUN:
                # Zero-cost plan preview: no DB write, no email, no fabricated ids -- report
                # that this row WOULD create an approval request and stop here.
                logger.info(f"      {label} -> dry-run: would create approval request '{request_name}' with 1 designation")
                return RowOutcome(
                    excel_row=row,
                    result=RowResult.WOULD_CREATE,
                    request_name=request_name,
                    designation_count=1,
                )

            item = build_approval_request_item(role_mapping, plan)
            approval_request = await with_retry(
                create_approval_request_with_item,
                request_name, role_mapping.state_center_id, role_mapping.department_id,
                role_mapping.state_center_name, role_mapping.department_name, role_mapping.org_type,
                row.mdo_id, item,
                description=f"create_approval_request_with_item(row {row.row_number})",
            )
            request_id = approval_request.id

            logger.info(f"      {label} -> approval request '{request_name}' ready, request_id={request_id}")

            await send_cbp_approval_email(row.mdo_id, request_name, str(USER_ID), str(request_id), label)

            logger.info(f"DONE  {label} -> approval request submitted successfully")
            return RowOutcome(
                excel_row=row,
                result=RowResult.SUCCEEDED,
                approval_request_id=request_id,
                request_name=request_name,
                designation_count=1,
                approval_request_item_ids=[item["id"]],
            )

        except Exception as exc:
            error_message = f"Unexpected failure: {exc}"
            logger.exception(f"FAIL  {label} -> {error_message}")
            return RowOutcome(excel_row=row, result=RowResult.FAILED, error=error_message)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

async def main():
    logger.info("=" * 100)
    logger.info("Batch: send bulk approval requests")
    logger.info(f"Excel file: {EXCEL_FILE}")
    logger.info(f"User ID: {USER_ID}")
    logger.info(f"Max concurrency: {MAX_CONCURRENCY}")
    logger.info(f"Log file: {LOG_FILE}")
    if DRY_RUN:
        logger.info("DRY RUN: zero-cost plan preview -- no database writes, no email notifications. "
                     "Rows with a role_mapping + matching CBP plan are reported as WOULD_CREATE.")
    logger.info("=" * 100)

    excel_rows, skipped_input_rows = read_excel_rows(EXCEL_FILE)
    if not excel_rows:
        logger.warning("No data rows found in Excel. Nothing to do.")
        await engine.dispose()
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    progress = ProgressTracker(len(excel_rows))
    tasks = [process_row(row, semaphore, progress) for row in excel_rows]
    outcomes: List[RowOutcome] = await asyncio.gather(*tasks)

    succeeded = [o for o in outcomes if o.result == RowResult.SUCCEEDED]
    skipped_no_rm = [o for o in outcomes if o.result == RowResult.SKIPPED_ROLE_MAPPING_NOT_FOUND]
    skipped_no_plan = [o for o in outcomes if o.result == RowResult.SKIPPED_NO_CBP_PLAN]
    failed = [o for o in outcomes if o.result == RowResult.FAILED]
    would_create = [o for o in outcomes if o.result == RowResult.WOULD_CREATE]

    logger.info("=" * 100)
    logger.info("RUN SUMMARY")
    logger.info("=" * 100)
    logger.info(f"Excel rows read:              {len(excel_rows)}")
    logger.info(f"Skipped (invalid input row):  {len(skipped_input_rows)}")
    logger.info(f"Succeeded:                    {len(succeeded)}")
    logger.info(f"Skipped (role_mapping n/f):   {len(skipped_no_rm)}")
    logger.info(f"Skipped (no CBP plan):        {len(skipped_no_plan)}")
    logger.info(f"Failed:                       {len(failed)}")
    if DRY_RUN:
        logger.info(f"Would create (dry-run):       {len(would_create)}")

    if skipped_input_rows:
        logger.info("-" * 100)
        logger.info("SKIPPED INVALID ROW DETAILS (also in the outcome CSV as SKIPPED_INVALID_ROW):")
        for s in skipped_input_rows:
            logger.info(
                f"  - row={s.row_number} role_mapping_id={s.raw_role_mapping_id!r} "
                f"recommendation_id={s.raw_recommendation_id!r} mdo_id={s.raw_mdo_id!r} reason={s.reason}"
            )

    if failed:
        logger.info("-" * 100)
        logger.info("FAILED DETAILS:")
        for o in failed:
            logger.info(
                f"  - row={o.excel_row.row_number} role_mapping_id={o.excel_row.role_mapping_id} "
                f"recommendation_id={o.excel_row.recommendation_id} mdo_id={o.excel_row.mdo_id} error={o.error}"
            )

    # Outcome summary written as CSV: one row per input row, echoing the input columns
    # plus approval_request_id, request_name, designation_count, approval_request_item_ids,
    # status, and error.
    with open(OUTCOME_CSV_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "state_center_id", "department_id", "org_type", "state_center_name",
            "department_name", "designation", "role_mapping_id", "recommendation_id",
            "mdo_id", "approval_request_id", "request_name", "designation_count",
            "approval_request_item_ids", "status", "error",
        ])
        for o in outcomes:
            row = o.excel_row
            writer.writerow([
                row.state_center_id,
                row.department_id,
                row.org_type,
                row.state_center_name,
                row.department_name,
                row.designation,
                str(row.role_mapping_id),
                str(row.recommendation_id),
                row.mdo_id,
                str(o.approval_request_id) if o.approval_request_id else "",
                o.request_name or "",
                o.designation_count,
                ",".join(str(i) for i in o.approval_request_item_ids) if o.approval_request_item_ids else "",
                o.result.value,
                o.error or "",
            ])

        for skipped in skipped_input_rows:
            writer.writerow([
                skipped.state_center_id,
                skipped.department_id,
                skipped.org_type,
                skipped.state_center_name,
                skipped.department_name,
                skipped.designation,
                skipped.raw_role_mapping_id,
                skipped.raw_recommendation_id,
                skipped.raw_mdo_id,
                "",
                "",
                0,
                "",
                "SKIPPED_INVALID_ROW",
                f"row {skipped.row_number}: {skipped.reason}",
            ])

    logger.info("=" * 100)
    logger.info(f"Full log written to: {LOG_FILE}")
    logger.info(f"Outcome CSV written to: {OUTCOME_CSV_FILE}")
    logger.info("=" * 100)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

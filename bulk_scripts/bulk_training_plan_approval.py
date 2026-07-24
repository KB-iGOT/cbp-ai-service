"""Bulk PUBLISH existing Training Plan approval requests (one per designation) directly via the CB ext
course service's AICBP APIs, talking DIRECTLY to the shared Postgres database — no portal/MDO HTTP
service required.

This is the standalone, self-contained equivalent of running the MDO-service
`POST /v1/mdo/approval-requests/publish` (approve+publish) against approval_requests that ALREADY EXIST
(created earlier by the portal or another script). There is no CREATE step here -- every row must
reference an existing approval_request_id:

  1. VALIDATE: approval_request_id must be present and a valid UUID (else SKIPPED_INVALID_ROW), and must
     resolve to a real approval_requests row scoped to --user-id (else FAILED). If found but its status
     isn't PENDING, it's either already fully APPROVED (ALREADY_APPROVED) or in some other terminal state
     like REJECTED/DRAFT (SKIPPED_NOT_PENDING) -- either way, nothing is published.
  2. PUBLISH (mirrors the MDO publish controller): lock the PENDING request (SELECT … FOR UPDATE), and for
     each PENDING item call `POST {CB_EXT_COURSE_SERVICE_URL}/cbplan/v2/aicbp/create` + `/aicbp/publish`
     directly. ONLY an item whose create+publish BOTH succeed gets an mdo_approval row (with the returned
     igot_cbp_plan_id) and is flipped to APPROVED; a failed item gets no DB write at all and stays PENDING,
     so the next run's PENDING-items query retries exactly it. The request itself is flipped to APPROVED
     only once every one of its items has succeeded (across however many runs that took).

Result is BINARY per row: APPROVED (every item published) or FAILED (any item failed, or the request/
publish call errored) -- there is no partial/in-between status. The remaining RowResult members
(ALREADY_APPROVED, WOULD_APPROVE, SKIPPED_*) are all "nothing was attempted" skips, not failures.

Real HTTP calls in the active path are: the CB ext course service's create/publish endpoints, and (when
ENABLE_EMAIL_NOTIFICATION is on) the notification service's approval email, sent after each row is
APPROVED -- mirroring the MDO controller's approval email. Everything else is done in-process against
the DB.

DB access: an async SQLAlchemy engine over the shared DATABASE_URL (asyncpg DSN). Minimal ORM models for the
touched tables are defined IN THIS FILE (nothing is imported from `src` or `mdo_code`).

Auth for the CB ext course service calls: just `x-authenticated-user-token: {user_token}` (the approver's
user JWT) + Content-Type -- no service Authorization token, no org/rootorg headers.
published_by (recorded in the outcome CSV) is extracted from the fetched user_token's own `sub` claim --
NOT from --user-id. --user-id is the separate value approval_requests.user_id is matched against when
looking up a row -- a request that exists but belongs to a different user_id is FAILED.

The approver user_token is ALWAYS fetched fresh via the SSO OIDC password grant (POST
{SUNBIRD_SSO_URL}/auth/realms/{SUNBIRD_SSO_REALM}/protocol/openid-connect/token with client_id/
client_secret/username/password) exactly ONCE at script startup -- there is no env var to supply/bypass it
with. All of DATABASE_URL, CB_EXT_COURSE_SERVICE_URL, NOTIFICATION_BASE_URL, ENABLE_EMAIL_NOTIFICATION,
SUNBIRD_SSO_URL, SUNBIRD_SSO_REALM, TOKEN_CLIENT_ID, TOKEN_CLIENT_SECRET, TOKEN_USERNAME, TOKEN_PASSWORD
are MANDATORY environment variables (no CLI flags, no silent defaults) -- set them in .env or the
environment; the script exits with a clear error naming the missing variable. (TOKEN_PASSWORD/
TOKEN_CLIENT_SECRET are secrets -- never commit real values, keep them in a git-ignored .env.)
ENABLE_EMAIL_NOTIFICATION ("true"/"false"/"1"/"0"/"yes"/"no") gates whether an approval email is sent
for each APPROVED row (via the notification service at NOTIFICATION_BASE_URL) -- when disabled, rows are
still published exactly the same, just without the email.

cbp_plan_name / due_date are REQUIRED by the iGOT create + the mdo_approval row. --due-date is a mandatory
CLI arg (the default for rows without their own due_date column). cbp_plan_name is prepared at REQUEST
LEVEL ONLY (never from the input row): "AI CBP for <designation>", using the request's own designation
(each approval_request is for exactly one designation) -- a request with no items to derive a designation
from is FAILED (no iGOT call is made with a missing name).

RETRY: transient iGOT failures (network/timeout, HTTP 429/500/502/503/504) are retried with exponential
backoff (--max-retries). A publish that permanently fails leaves the item AND its request PENDING and
writes no mdo_approval row (the shared DB enum has no FAILED value); a re-run retries the still-PENDING
request. The request is flipped to APPROVED only once every item has published.

IDEMPOTENT RE-RUNS: there is no local state file -- the DB itself is the source of truth. Re-running the
script against the same rows is safe: a request already fully APPROVED is reported ALREADY_APPROVED (no
writes), and a request with some items still PENDING (from a prior partial failure) picks up exactly
where it left off via the same PENDING-items query.

The input file's 'approval_request_id' column is MANDATORY on every row -- a row with a blank or invalid
value is skipped (status=skipped_invalid_row), not fatal to the rest of the batch.

DRY-RUN by default (no writes, no iGOT calls -- just reports which rows would be published). Pass
--execute to actually publish.

OUTPUT: exactly ONE outcome CSV is written per run (dry-run or --execute), alongside the input file --
no separate failures/audit JSON files. Columns: every column from the input file itself (in its own
original order), followed by cbp_plan_name, cbp_plan_id, due_date, status, error, published_by.

Run (dry-run; user_token is always fetched fresh from SSO at startup):
    python bulk_scripts/bulk_training_plan_approval.py --excel plans.xlsx --user-id <uuid> --due-date 2027-03-31
Run (execute):
    ... --execute
"""
import argparse
import asyncio
import base64
import binascii
import csv
import enum
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import (
    Column, DateTime, Enum as SAEnum, ForeignKey, Integer, String, Text, and_, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, relationship, selectinload

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger("bulk_training_plan_approval")


def _load_dotenv():
    """Populate os.environ from a .env file (cwd first, then this file's repo root) so DATABASE_URL /
    CB_EXT_COURSE_SERVICE_URL (and the other settings below) can be read without CLI flags — mirrors
    script 2's config loading. Real environment variables win (never overwritten). Uses python-dotenv if
    installed, otherwise a minimal hand-parser."""
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


_load_dotenv()  # so require_env() below can read values populated from .env


def require_env(name: str) -> str:
    """Fetches a mandatory environment variable, raising a clear, actionable error (instead of a bare
    KeyError) if it's missing from both the environment and .env -- every config value this script
    depends on is required now, there are no silent defaults."""
    value = os.environ.get(name)
    if not value:
        sys.exit(
            f"Aborting: missing required environment variable '{name}'. "
            f"Set it in .env or in the environment before running this script."
        )
    return value


DEFAULT_BATCH_SIZE = 10
DEFAULT_TIMEOUT = 120  # seconds; iGOT publish is slow.
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 2.0  # seconds; exponential: backoff * 2**attempt.

# Logs/output live under bulk_scripts/logs/ (relative to this file, not the CWD), timestamped per run --
# same convention as the sibling scripts (batch_generate_and_save_cbp_plan.py etc).
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"bulk_training_plan_approval_{RUN_TIMESTAMP}.log"

_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
logger.setLevel(logging.INFO)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(_formatter)
logger.addHandler(_file_handler)

_NON_FAILURE_STATUSES = {
    "approved", "already_approved", "skipped_invalid_row", "skipped_not_pending", "would_approve",
}

RETRYABLE_HTTP = {429, 500, 502, 503, 504}

HEADER_MAP = {
    "duedate": "due_date",
    "designationname": "designation_name",
    "designation": "designation_name",
    "approvalrequestid": "approval_request_id",
}

# Appended after the input file's own columns in the outcome CSV -- row_no/approval_request_id/
# designation_name are dropped here since they're already present among the input columns.
REPORT_COLUMNS = ["cbp_plan_name", "cbp_plan_id", "due_date", "status", "error", "published_by"]


@dataclass
class Config:
    """Run-wide configuration, built once in main() and threaded through every helper -- replaces the
    previous loose `cfg` dict so every field is named/typed in one place instead of scattered
    cfg["..."] string-keyed lookups."""
    execute: bool
    cb_ext_course_base: str
    user_token: str
    user_id: uuid.UUID
    published_by: str  # user id extracted from user_token's `sub` claim (NOT --user-id).
    default_due_date: str
    max_retries: int
    backoff: float
    # notify is read from ENABLE_EMAIL_NOTIFICATION (env) -- process_row calls send_approval_email for
    # each APPROVED row only when this is true.
    notify: bool
    notification_base: str
    approver_name: str = ""


# ─────────────────────────────────────── ORM models (standalone) ───────────────────────────────────────
# Minimal mirrors of the shared tables — enough columns for create + publish. Enum columns reuse the
# EXISTING Postgres enum types (create_type=False); the stored labels are the UPPERCASE member names.

Base = declarative_base()


# These mirror the ACTUAL Postgres enum labels (verified against the shared DB), not the app's Python
# enums — the request- and item-level enums differ, and neither has FAILED:
#   approval_status_enum              => DRAFT, PENDING, APPROVED, REJECTED
#   approval_request_item_status_enum => PENDING, APPROVED, REJECTED
# SQLAlchemy maps a Column(Enum(PyEnum)) to the DB using the member NAMES, so the names below must equal
# the DB labels exactly. Do not add values the DB type doesn't have (writing them raises
# InvalidTextRepresentationError).
class ApprovalStatusEnum(str, enum.Enum):
    """Request-level status (approval_status_enum)."""
    DRAFT = "DRAFT"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalItemStatusEnum(str, enum.Enum):
    """Status for individual approval request items (designations)"""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id = Column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_name = Column(String)
    user_id = Column(PgUUID(as_uuid=True))
    org_type = Column(String)
    state_center_id = Column(String)
    department_id = Column(String)
    state_center_name = Column(String)
    department_name = Column(String)
    mdo_id = Column(String)
    designation_count = Column(Integer, default=0)
    status = Column(SAEnum(ApprovalStatusEnum, name="approval_status_enum", create_type=False),
                    default=ApprovalStatusEnum.PENDING)
    rejected_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    reviewer_comments = Column(Text)
    items = relationship("ApprovalRequestItem", lazy="selectin", cascade="all, delete-orphan",
                         backref="approval_request")


class ApprovalRequestItem(Base):
    __tablename__ = "approval_request_items"
    id = Column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_request_id = Column(PgUUID(as_uuid=True), ForeignKey("approval_requests.id"))
    source_role_mapping_id = Column(PgUUID(as_uuid=True))
    designation_name = Column(String)
    wing_division_section = Column(String)
    role_responsibilities = Column(JSONB)
    activities = Column(JSONB)
    competencies = Column(JSONB)
    sort_order = Column(Integer)
    igot_designation_name = Column(String)
    igot_designation_id = Column(String)
    cbp_plan_data = Column(JSONB)
    status = Column(SAEnum(ApprovalItemStatusEnum, name="approval_request_item_status_enum", create_type=False),
                    default=ApprovalItemStatusEnum.PENDING)
    reviewer_comments = Column(Text)
    rejected_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MdoApproval(Base):
    __tablename__ = "mdo_approval"
    id = Column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_request_id = Column(PgUUID(as_uuid=True), ForeignKey("approval_requests.id"))
    approval_request_item_id = Column(PgUUID(as_uuid=True), ForeignKey("approval_request_items.id"))
    plan_name = Column(String)
    due_date = Column(DateTime(timezone=True))
    igot_cbp_plan_id = Column(PgUUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"
    user_id = Column(PgUUID(as_uuid=True), primary_key=True)
    email = Column(String)


# ─────────────────────────── CSV / cell helpers (from scripts 1 & 2) ───────────────────────────

def _clean(value):
    """Normalize a cell to a stripped string, or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _map_header(header):
    """Map a spreadsheet header to its internal field name (HEADER_MAP), tolerating case/whitespace."""
    if header is None:
        return ""
    norm = "".join(str(header).split()).lower()
    return HEADER_MAP.get(norm, str(header).strip())


def read_rows(path):
    """Read the input file into a list of dict rows keyed by internal field names (.xlsx or delimited text)."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header = [_map_header(h) for h in next(rows_iter)]
        except StopIteration:
            return []
        rows = []
        for values in rows_iter:
            if values is None or all(v is None for v in values):
                continue
            rows.append({header[i]: (values[i] if i < len(values) else None) for i in range(len(header))})
        return rows

    with open(path, newline="", encoding="utf-8-sig") as f:
        first_line = f.readline()
        f.seek(0)
        delimiter = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        rows = []
        for raw in reader:
            row = {_map_header(k): v for k, v in raw.items()}
            if all(_clean(v) is None for v in row.values()):
                continue
            rows.append(row)
        return rows


# ─────────────────────────────────── token / value helpers ───────────────────────────────────

def decode_jwt_payload(token):
    """Decode a JWT's payload segment (no signature verification). Raises ValueError on a malformed token."""
    if not token:
        raise ValueError("empty token")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a three-segment JWT")
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"payload is not valid base64url: {e}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"payload is not valid JSON: {e}")


def token_user_id(payload):
    """iGOT user id = last ':'-separated segment of the `sub` claim."""
    sub = payload.get("sub") or ""
    return sub.split(":")[-1] if sub else None


def check_token(name, token):
    """Decode + validate a JWT, returning (payload, user_id). SystemExit on malformed/expired token."""
    try:
        payload = decode_jwt_payload(token)
    except ValueError as e:
        sys.exit(f"Aborting: --{name} is not a valid JWT: {e}")
    exp = payload.get("exp")
    if exp is not None and float(exp) <= time.time():
        expired_at = datetime.fromtimestamp(float(exp), tz=timezone.utc).isoformat()
        sys.exit(f"Aborting: --{name} expired at {expired_at}. Refresh the token and re-run.")
    uid = token_user_id(payload)
    logger.info(f"{name}: user_id={uid}")
    return payload, uid


async def fetch_user_token(client, *, token_url, client_id, client_secret, username, password):
    """Obtain the approver's user_token via the Keycloak/OIDC password grant (mirrors the reference curl:
    POST form-urlencoded {grant_type=password, client_id, client_secret, username, password}). Called
    exactly ONCE per script run, at startup. Returns the `access_token` string. SystemExit on any failure
    (without a token there is nothing to run). Secrets are never logged — only the username and endpoint
    are."""
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": username,
        "password": password,
    }
    try:
        resp = await client.post(
            token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
    except httpx.HTTPError as e:
        sys.exit(f"Aborting: could not reach the SSO token endpoint {token_url}: {e}")
    if resp.status_code != 200:
        sys.exit(f"Aborting: SSO token request failed ({resp.status_code}): {_http_detail(resp)}")
    try:
        access_token = resp.json().get("access_token")
    except (json.JSONDecodeError, ValueError):
        access_token = None
    if not access_token:
        sys.exit("Aborting: SSO token response contained no access_token.")
    return access_token


def normalize_due_date(value):
    """Normalize a due_date to an ISO datetime string. A bare YYYY-MM-DD becomes midnight UTC."""
    s = _clean(value)
    if not s:
        raise ValueError("due_date is empty")
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        pass
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _http_detail(resp):
    """Short single-line detail for the report from an httpx.Response."""
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        text = detail if detail is not None else json.dumps(body, ensure_ascii=False, default=str)
    except (json.JSONDecodeError, ValueError):
        text = resp.text
    return " ".join(str(text).split())[:500]


def extract_content_ids(cbp_plan_data_list):
    """Dedup course identifiers from cbp_plan_data[].selected_courses[].identifier (mirrors igot_service)."""
    seen = set()
    content_ids = []
    if not cbp_plan_data_list:
        return content_ids
    records = cbp_plan_data_list if isinstance(cbp_plan_data_list, list) else [cbp_plan_data_list]
    for record in records:
        if not isinstance(record, dict):
            continue
        for course in record.get("selected_courses", []) or []:
            identifier = course.get("identifier") if isinstance(course, dict) else None
            if identifier and identifier not in seen:
                seen.add(identifier)
                content_ids.append(identifier)
    return content_ids


# ─────────────────────────────────────── retrying POST (iGOT) ───────────────────────────────────────
# HTTP-specific retry helper (status-code + transport-error retry) -- the same exponential-backoff shape
# as the sibling scripts' with_retry, but built for inspecting an httpx.Response rather than retrying an
# arbitrary coroutine on any exception.

async def with_retry(client, url, payload, headers, *, description, max_retries, backoff):
    """POST with retry on transport errors + RETRYABLE_HTTP (exponential backoff). A 401/403 is returned
    as-is without retry (the caller reports it as a normal failure). Returns
    (response|None, transport_error|None, attempts)."""
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            if attempts > max_retries:
                return None, f"request error after {attempts} attempt(s): {e}", attempts
            delay = backoff * (2 ** (attempts - 1))
            logger.warning(f"  [retry] {description} failed on attempt {attempts}/{max_retries + 1} "
                           f"(transport error): {e}. Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
            continue

        if resp.status_code in (401, 403):
            return resp, None, attempts
        if resp.status_code in RETRYABLE_HTTP and attempts <= max_retries:
            delay = backoff * (2 ** (attempts - 1))
            logger.warning(f"  [retry] {description} failed on attempt {attempts}/{max_retries + 1} "
                           f"(HTTP {resp.status_code}). Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
            continue
        return resp, None, attempts


# ───────────────────────────────── CB ext course service: create / publish ─────────────────────────────

def _cb_ext_course_headers(cfg):
    """Header set for the CB ext course service's AICBP calls: just the user token + content type --
    no service Authorization / org / rootorg headers (unlike the old iGOT KB endpoints)."""
    return {
        "Content-Type": "application/json",
        "x-authenticated-user-token": cfg.user_token,
    }


async def call_igot_create(client, cfg, org_id, plan_name, due_date, designation, content_ids):
    """POST {CB_EXT_COURSE_SERVICE_URL}/cbplan/v2/aicbp/create. Returns (plan_id|None, error)."""
    url = f"{cfg.cb_ext_course_base}/cbplan/v2/aicbp/create"
    payload = {
        "request": {
            "comment": f"{plan_name} is created",
            "contentList": content_ids,
            "contentType": "Course",
            "contextData": {
                "accessControl": {
                    "userGroups": [
                        {
                            "userGroupName": "User Group 1",
                            "userGroupCriteriaList": [
                                {"criteriaKey": "service", "criteriaValue": [designation]},
                                {"criteriaKey": "rootOrgId", "criteriaValue": [org_id]},
                            ],
                        }
                    ],
                    "version": 1,
                }
            },
            "endDate": due_date.strftime("%Y-%m-%d"),
            "isApar": False,
            "name": plan_name,
            "targetedOrganisation": org_id,
            "planType": "AICBP",
        }
    }
    resp, err, _att = await with_retry(client, url, payload, _cb_ext_course_headers(cfg),
                                       description="cb-ext-course-create", max_retries=cfg.max_retries,
                                       backoff=cfg.backoff)
    if resp is None:
        return None, err
    if resp.status_code // 100 != 2:
        return None, f"create {resp.status_code}: {_http_detail(resp)}"
    try:
        plan_id = resp.json().get("result", {}).get("id")
    except (json.JSONDecodeError, ValueError):
        plan_id = None
    if not plan_id:
        return None, "create returned no result.id"
    return str(plan_id), None


async def call_igot_publish(client, cfg, org_id, plan_id):
    """POST {CB_EXT_COURSE_SERVICE_URL}/cbplan/v2/aicbp/publish. Returns (ok, error)."""
    url = f"{cfg.cb_ext_course_base}/cbplan/v2/aicbp/publish"
    payload = {"request": {"id": plan_id, "comment": "CBP plan approved", "targetedOrganisation": org_id}}
    resp, err, _att = await with_retry(client, url, payload, _cb_ext_course_headers(cfg),
                                       description="cb-ext-course-publish", max_retries=cfg.max_retries,
                                       backoff=cfg.backoff)
    if resp is None:
        return False, err
    if resp.status_code // 100 != 2:
        return False, f"publish {resp.status_code}: {_http_detail(resp)}"
    return True, None


async def publish_single_item(client, cfg, item, org_id, plan_name, due_date_obj):
    """Create + publish one item's CBP plan on iGOT. Returns the per-item result dict (mirrors the
    controller's _publish_single_item shape)."""
    designation = item.igot_designation_name or item.designation_name
    content_ids = extract_content_ids(item.cbp_plan_data)
    if not content_ids:
        return {"item_id": str(item.id), "designation_name": designation, "status": "failed",
                "plan_id": None, "error": "No CBP Plan found for this item."}

    plan_id, err = await call_igot_create(client, cfg, org_id, plan_name, due_date_obj,
                                          designation, content_ids)
    if not plan_id:
        return {"item_id": str(item.id), "designation_name": designation, "status": "failed",
                "plan_id": None, "error": err or "iGOT create failed"}

    ok, err = await call_igot_publish(client, cfg, org_id, plan_id)
    if not ok:
        return {"item_id": str(item.id), "designation_name": designation, "status": "failed",
                "plan_id": None, "error": err or "iGOT publish failed"}

    return {"item_id": str(item.id), "designation_name": designation, "status": "success",
            "plan_id": plan_id, "error": None}


# ─────────────────────────────── DB + iGOT: publish (mirrors MDO controller) ───────────────────────────

async def _get_request_for_update(session, request_id, user_id):
    """Looks up the approval_request by id, scoped to --user-id (the request's owner), and locks it
    (SELECT ... FOR UPDATE) for the duration of the publish transaction."""
    stmt = (
        select(ApprovalRequest)
        .options(selectinload(ApprovalRequest.items))
        .where(and_(ApprovalRequest.id == request_id, ApprovalRequest.user_id == user_id))
        .with_for_update()
    )
    return (await session.execute(stmt)).scalars().first()


async def _get_request_readonly(session, request_id, user_id):
    """Same lookup as _get_request_for_update but without locking -- used for the dry-run plan-name
    preview, which makes no writes and so needs no row lock."""
    stmt = (
        select(ApprovalRequest)
        .options(selectinload(ApprovalRequest.items))
        .where(and_(ApprovalRequest.id == request_id, ApprovalRequest.user_id == user_id))
    )
    return (await session.execute(stmt)).scalars().first()


async def _persist_approval_per_item(session, request_id, user_id, plan_name, due_date_obj, all_items,
                                     item_results):
    """Persist the publish outcome. ONLY items whose iGOT create+publish both succeeded are written --
    a failed item gets NO mdo_approval row and stays PENDING, so it is naturally retried on the next run
    (no special-cased retry-handle placeholder needed). The request is flipped to APPROVED only when
    EVERY item on the request (not just this round's pending subset) ends up APPROVED -- i.e. only once
    every item has actually succeeded at least once across however many runs it took."""
    now = datetime.now(timezone.utc)
    due_dt = datetime.combine(due_date_obj, datetime.min.time()).replace(tzinfo=timezone.utc)
    result_map = {r["item_id"]: r for r in item_results}

    for item in all_items:
        res = result_map.get(str(item.id))
        if not res or res["status"] != "success":
            continue  # failed/not-attempted item: no DB write, stays PENDING for retry.
        session.add(MdoApproval(
            approval_request_id=request_id, approval_request_item_id=item.id,
            plan_name=plan_name, due_date=due_dt,
            igot_cbp_plan_id=uuid.UUID(res["plan_id"]), created_at=now,
        ))
        await session.execute(update(ApprovalRequestItem).where(ApprovalRequestItem.id == item.id)
                              .values(status=ApprovalItemStatusEnum.APPROVED))

    # Flip the request to APPROVED only if no item remains PENDING (i.e. every item, across however
    # many runs it took, has now succeeded). Re-check from the DB rather than the in-memory `all_items`
    # snapshot, since this same transaction just updated some of their statuses above.
    remaining_pending = (await session.execute(
        select(ApprovalRequestItem.id)
        .where(ApprovalRequestItem.approval_request_id == request_id,
               ApprovalRequestItem.status == ApprovalItemStatusEnum.PENDING)
    )).first()
    if remaining_pending is None:
        await session.execute(
            update(ApprovalRequest)
            .where(and_(ApprovalRequest.id == request_id, ApprovalRequest.user_id == user_id,
                        ApprovalRequest.status == ApprovalStatusEnum.PENDING))
            .values(status=ApprovalStatusEnum.APPROVED, updated_at=now)
        )
    await session.commit()


def _build_plan_name(request) -> Optional[str]:
    """CBP plan name, prepared at request level only (never from the input row): "AI CBP for
    <designation>", using the request's own designation (each approval_request is for exactly one
    designation, per the one-request-per-designation model), truncated to iGOT's 70-character cap.
    None if the request has no items to derive a designation from."""
    item = request.items[0] if request.items else None
    designation = (item.igot_designation_name or item.designation_name) if item else None
    if not designation:
        return None
    plan_name = f"AI CBP for {designation}"
    return plan_name[:70].rstrip() if len(plan_name) > 70 else plan_name


async def approve_and_publish(session, client, cfg, request_id, due_date_obj):
    """Publish a PENDING request. Returns (outcome, item_results, plan_name) where outcome in
    {published, already_approved, request_not_found, not_pending}.

    `_persist_approval_per_item` only marks items APPROVED (and only flips the request to APPROVED) once
    they've actually succeeded on iGOT -- a failed item stays PENDING, so a re-run naturally retries just
    the still-failing items via this same PENDING-items query. The failure (including a 401/403 from
    iGOT) is still surfaced in the outcome CSV for this run."""
    request = await _get_request_for_update(session, request_id, cfg.user_id)
    if request is None:
        await session.rollback()
        return "request_not_found", [], None
    if request.status != ApprovalStatusEnum.PENDING:
        await session.rollback()
        return ("already_approved" if request.status == ApprovalStatusEnum.APPROVED else "not_pending"), [], None

    org_id = request.department_id or request.state_center_id
    pending_items = [it for it in request.items if it.status == ApprovalItemStatusEnum.PENDING]

    plan_name = _build_plan_name(request)
    if not plan_name:
        await session.rollback()
        return "no_designation", [], None

    item_results = []
    for item in pending_items:
        item_results.append(await publish_single_item(client, cfg, item, org_id, plan_name,
                                                       due_date_obj))
    if not item_results:
        await session.rollback()
        return "already_approved", [], plan_name
    await _persist_approval_per_item(session, request_id, cfg.user_id, plan_name, due_date_obj,
                                     request.items, item_results)
    return "published", item_results, plan_name


# ────────────────────────────────────── notification (approval email) ──────────────────────────────────
# Embedded so the script stays standalone. The notification service substitutes the $-placeholders
# server-side: $status / $cbpName / $approverName / $actionDate / $rejectionReason / $meetingLink.
APPROVED_EMAIL_HTML = r"""<!doctype html>
<html style="font-family:Lato,Helvetica,Arial,sans-serif">
<body style="font-family:Lato,Helvetica,Arial,sans-serif;background-color:#f7f7f7;color:#4d4d4d;margin:0">
    <table cellpadding="0" cellspacing="0" width="100%" align="center"
        style="max-width:600px;border:2px solid #ddd;border-collapse:collapse;font-family:Lato,Helvetica,Arial,sans-serif">
        <tr><td style="padding:16px">
            <p style="font-weight:700;font-size:16px;margin:0 0 16px 0">Namaste Karmayogi,</p>
            <p style="font-size:14px;line-height:20px;margin:0 0 16px 0">
                Your AI-generated Capacity Building Plan (CBP) request has been $status.</p>
            <p style="font-size:14px;line-height:20px;margin:0 0 8px 0">Details:</p>
            <ul style="font-size:14px;line-height:20px;margin:0 0 16px 20px;padding:0">
                <li>CBP/request Name: $cbpName</li>
                <li>Status: $status</li>
                <li>Action Taken By: $approverName</li>
                <li>Action Date: $actionDate</li>
            </ul>
            <p style="font-size:14px;line-height:20px;margin:0 0 16px 0">Reason for Rejection: $rejectionReason</p>
            <p style="font-size:14px;line-height:20px;margin:0 0 16px 0">
                You may log in to the AI CBP tool to review the latest status.</p>
            <p style="font-size:14px;line-height:20px;margin:0">Regards,<br>AI CBP Platform Team</p>
        </td></tr>
    </table>
</body>
</html>
"""


async def resolve_requestor_email(session, user_id):
    """Read the requestor's email straight from the users table (mirrors _send_cbplan_status_email)."""
    if not user_id:
        return None
    try:
        email = (await session.execute(select(User.email).where(User.user_id == user_id))).scalar_one_or_none()
    except Exception as e:  # noqa: BLE001 - notification is best-effort
        logger.warning(f"Could not read requestor email for user {user_id}: {e}")
        return None
    return email


async def send_approval_email(client, cfg, recipient, plan_name):
    """Best-effort: POST the 'approved' email to the notification service. Returns 'yes'/'no'. Never raises.
    Called from process_row for each APPROVED row, only when cfg.notify (ENABLE_EMAIL_NOTIFICATION) is
    true."""
    if not (cfg.notify and cfg.notification_base and recipient):
        return "no"
    action_date = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    cbp_name = plan_name or "your Capacity Building Plan"
    params = {
        "status": "approved",
        "cbpName": cbp_name,
        "approverName": cfg.approver_name or "MDO Approver",
        "actionDate": action_date,
        "rejectionReason": "Not applicable",
        "meetingLink": "",
    }
    payload = {
        "request": {
            "notifications": [{
                "type": "email", "priority": 1, "ids": [recipient], "bccIds": [],
                "action": {
                    "type": "email", "category": "email",
                    "createdBy": {"id": cfg.published_by or "", "type": "user"},
                    "template": {
                        "data": APPROVED_EMAIL_HTML, "id": "cbp-plan-approved", "params": params,
                        "type": "email",
                        "config": {"subject": f"{cbp_name[0].upper() + cbp_name[1:]} has been approved",
                                   "sender": ""},
                    },
                },
            }]
        }
    }
    resp, err, _ = await with_retry(client, cfg.notification_base + "/v2/notification/send",
                                    payload, {"Content-Type": "application/json"},
                                    description=f"notify[{recipient}]", max_retries=cfg.max_retries,
                                    backoff=cfg.backoff)
    if resp is not None and 200 <= resp.status_code < 300:
        logger.info(f"Approval email sent to <{recipient}>")
        return "yes"
    reason = err or (f"HTTP {resp.status_code}: {_http_detail(resp)}" if resp is not None else "unknown")
    logger.warning(f"Approval email to <{recipient}> failed: {reason}")
    return "no"


# ────────────────────────────────────── per-row processing ─────────────────────────────────────

class RowResult(str, enum.Enum):
    """Binary publish outcome -- an approval_request is either fully published (every item's iGOT
    create+publish succeeded) or it FAILED (any item failed); there is no partial/in-between status.
    The remaining members are all "nothing was attempted" skips, not failures."""
    APPROVED = "approved"
    FAILED = "failed"
    ALREADY_APPROVED = "already_approved"
    WOULD_APPROVE = "would_approve"
    SKIPPED_INVALID_ROW = "skipped_invalid_row"
    SKIPPED_NOT_PENDING = "skipped_not_pending"


@dataclass
class RowOutcome:
    row_no: int
    result: RowResult
    error: str = ""
    approval_request_id: str = ""
    designation_name: str = ""
    cbp_plan_name: str = ""
    cbp_plan_id: str = ""
    due_date: str = ""
    published_by: str = ""
    raw_row: dict = None  # the input row's own columns, echoed into the outcome CSV as-is.


class ProgressTracker:
    """Assigns each row a stable [index/total] label as it starts, mirroring the pattern used in
    batch_generate_and_save_cbp_plan.py / batch_send_approval_requests.py."""

    def __init__(self, total: int):
        self.total = total
        self._count = 0

    def next_index(self) -> int:
        self._count += 1
        return self._count


async def process_row(session_factory, client, row, cfg,
                      progress: "ProgressTracker") -> RowOutcome:
    """Publish one existing approval_request's Training Plan directly via DB + iGOT (no CREATE step --
    the request + its item(s) must already exist). Returns a RowOutcome (also used to build the CSV
    report row). No local resume/state tracking -- the DB itself is the source of truth for whether a
    request was already approved (approve_and_publish returns ALREADY_APPROVED for a non-PENDING
    request), so re-running the script against the same rows is naturally idempotent without it."""
    index = progress.next_index()
    designation_name = _clean(row.get("designation_name"))
    csv_request_id = _clean(row.get("approval_request_id"))

    label = f"[{index}/{progress.total}] approval_request_id={csv_request_id or '?'} designation={designation_name or '?'}"
    logger.info(f"START {label}")

    result = RowOutcome(
        row_no=index, result=RowResult.FAILED,
        published_by=cfg.published_by, designation_name=designation_name or "",
        approval_request_id=csv_request_id or "", raw_row=row,
    )

    def finish(res: RowResult, error="", **fields) -> RowOutcome:
        for k, v in fields.items():
            setattr(result, k, v)
        result.result = res
        result.error = error
        log_line = f"{res.value} {label}" + (f" -> {error}" if error else "")
        if res in (RowResult.APPROVED, RowResult.WOULD_APPROVE):
            logger.info(f"DONE  {log_line}")
        elif res in (RowResult.SKIPPED_INVALID_ROW, RowResult.SKIPPED_NOT_PENDING, RowResult.ALREADY_APPROVED):
            logger.info(f"SKIP  {log_line}")
        else:
            logger.warning(f"FAIL  {log_line}")
        return result

    # ── approval_request_id is the ONLY thing driving a row now -- blank/invalid is a skip, not fatal ──
    if not csv_request_id:
        return finish(RowResult.SKIPPED_INVALID_ROW, "missing approval_request_id")
    try:
        uuid.UUID(csv_request_id)
    except ValueError:
        return finish(RowResult.SKIPPED_INVALID_ROW, f"approval_request_id is not a UUID: {csv_request_id}")

    try:
        due_iso = normalize_due_date(_clean(row.get("due_date")) or cfg.default_due_date)
    except ValueError as e:
        return finish(RowResult.FAILED, f"bad_due_date: {e}")
    due_date_obj = datetime.fromisoformat(due_iso).date()
    result.due_date = due_iso

    # ── Dry-run: read-only validation, no writes / iGOT. Still looks up the request (no row lock)
    # to preview the request-level cbp_plan_name in the CSV. ──
    if not cfg.execute:
        async with session_factory() as session:
            request = await _get_request_readonly(session, uuid.UUID(csv_request_id), cfg.user_id)
        plan_name = _build_plan_name(request) if request else None
        return finish(RowResult.WOULD_APPROVE, f"would publish request {csv_request_id}",
                     approval_request_id=csv_request_id, cbp_plan_name=plan_name or "")

    approval_request_id = csv_request_id
    result.approval_request_id = approval_request_id

    # ── PUBLISH (+ retry of any FAILED items on re-run) ──
    try:
        async with session_factory() as session:
            outcome, item_results, plan_name = await approve_and_publish(
                session, client, cfg, uuid.UUID(str(approval_request_id)), due_date_obj)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"publish failed for '{label}'")
        return finish(RowResult.FAILED, f"db/igot error: {e}")

    if plan_name:
        result.cbp_plan_name = plan_name

    if outcome == "request_not_found":
        return finish(RowResult.FAILED, "no approval_request found for this id + user_id")
    if outcome == "no_designation":
        return finish(RowResult.FAILED, "request has no items to derive a CBP plan name (designation) from")
    if outcome == "not_pending":
        return finish(RowResult.SKIPPED_NOT_PENDING,
                      "request exists but its status is not PENDING (REJECTED/DRAFT) -- skipped")
    if outcome == "already_approved":
        return finish(RowResult.ALREADY_APPROVED, "request already approved; no pending/failed items")

    failed = [r for r in item_results if r.get("status") != "success"]
    plan_ids = [str(r.get("plan_id")) for r in item_results if r.get("plan_id")]

    # Binary result: ANY item failing makes the whole row FAILED (the successful items' mdo_approval
    # rows are still persisted -- only the reported status simplifies to a single pass/fail per row).
    if failed:
        errors = "; ".join(f"{r.get('designation_name')}: {r.get('error')}" for r in failed)
        return finish(RowResult.FAILED, errors or "some items failed", cbp_plan_id="; ".join(plan_ids))

    if cfg.notify:
        async with session_factory() as session:
            recipient = await resolve_requestor_email(session, cfg.user_id)
        await send_approval_email(client, cfg, recipient, plan_name)

    return finish(RowResult.APPROVED, "", cbp_plan_id="; ".join(plan_ids))


# ───────────────────────────────────────── reporting ─────────────────────────────────────────

def _input_columns(results: List[RowOutcome]) -> list:
    """Ordered union of the input file's own column names across all rows, in the order they first
    appear (i.e. the input file's original column order), excluding any name already covered by
    REPORT_COLUMNS (e.g. due_date, if a row happens to define its own) -- so the CSV shows every
    original input column exactly once, without duplicating the output columns derived from them."""
    seen = set(REPORT_COLUMNS)
    cols = []
    for r in results:
        for k in (r.raw_row or {}):
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols


def _report_row(r: RowOutcome, input_columns: list) -> list:
    raw = r.raw_row or {}
    return [
        *(raw.get(col) for col in input_columns),
        r.cbp_plan_name, r.cbp_plan_id, r.due_date, r.result.value, r.error, r.published_by,
    ]


def write_report_csv(results: List[RowOutcome], excel_path):
    """The SOLE outcome output: one CSV written alongside the input Excel/CSV file (same convention as
    the sibling scripts' OUTCOME_CSV_FILE), timestamped with this run's RUN_TIMESTAMP -- for both
    dry-run and --execute. No JSON files (failures log / audit log) are written. Every input column comes
    first (in the input file's own order), followed by the standard REPORT_COLUMNS outcome columns."""
    input_columns = _input_columns(results)
    out_dir = Path(excel_path).resolve().parent
    path = out_dir / f"bulk_training_plan_approval_{RUN_TIMESTAMP}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(input_columns + REPORT_COLUMNS)
        for r in results:
            writer.writerow(["" if v is None else str(v) for v in _report_row(r, input_columns)])
    return path


# ─────────────────────────────────────────── main ────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Bulk-publish existing Training Plan approval requests (one per designation) "
                     "directly via DB + iGOT. Does not create requests -- approval_request_id must "
                     "already exist."
    )
    parser.add_argument("--excel", required=True, help="Path to the source CSV/Excel input file. Mandatory. "
                                                        "Must contain an 'approval_request_id' column.")
    parser.add_argument("--user-id", required=True, type=uuid.UUID,
                        help="Owner user UUID -- each row's approval_request_id must belong to this "
                             "user (approval_requests.user_id), or that row is reported FAILED. Mandatory.")
    parser.add_argument("--due-date", required=True,
                        help="Default due_date for rows without a due_date column (YYYY-MM-DD or ISO datetime). "
                             "Mandatory.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Max rows in flight (default {DEFAULT_BATCH_SIZE}).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Per-iGOT-request timeout in seconds (default {DEFAULT_TIMEOUT}).")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES,
                        help=f"Retries for transient iGOT errors (default {DEFAULT_MAX_RETRIES}).")
    parser.add_argument("--retry-backoff", type=float, default=DEFAULT_RETRY_BACKOFF,
                        help=f"Base backoff seconds, exponential (default {DEFAULT_RETRY_BACKOFF}).")
    parser.add_argument("--execute", action="store_true",
                        help="Perform the real DB writes + iGOT calls. Default is a dry-run.")
    args = parser.parse_args()

    if not os.path.exists(args.excel):
        sys.exit(f"Input file not found: {args.excel}")

    # All external-service config is mandatory, read from env/.env only (no CLI overrides).
    database_url = require_env("DATABASE_URL")
    cb_ext_course_service_url = require_env("CB_EXT_COURSE_SERVICE_URL")
    notification_base_url = require_env("NOTIFICATION_BASE_URL")
    enable_email_notification = require_env("ENABLE_EMAIL_NOTIFICATION").strip().lower() in ("1", "true", "yes")

    # Approver user_token: ALWAYS fetched fresh from SSO via the password grant, once, at startup --
    # no env-var bypass. Every credential below is a mandatory env var.
    sso_base_url = require_env("SUNBIRD_SSO_URL")
    sso_realm = require_env("SUNBIRD_SSO_REALM")
    token_client_id = require_env("TOKEN_CLIENT_ID")
    token_client_secret = require_env("TOKEN_CLIENT_SECRET")
    token_username = require_env("TOKEN_USERNAME")
    token_password = require_env("TOKEN_PASSWORD")
    sso_base = sso_base_url.rstrip("/").removesuffix("/auth")
    token_url = f"{sso_base}/auth/realms/{sso_realm}/protocol/openid-connect/token"
    logger.info(f"Fetching approver user_token from {token_url} as {token_username}")
    async with httpx.AsyncClient(timeout=args.timeout) as _sso_client:
        user_token = await fetch_user_token(
            _sso_client, token_url=token_url, client_id=token_client_id,
            client_secret=token_client_secret, username=token_username,
            password=token_password,
        )
    logger.info("Approver user_token fetched from SSO.")

    # published_by is extracted from the token's own `sub` claim -- NOT from --user-id (which only
    # scopes the approval_request lookup to its owner).
    user_token_payload, published_by = check_token("user-token", user_token)
    if not published_by:
        sys.exit("Aborting: fetched user_token has no user id (sub) to record as published_by.")
    approver_name = user_token_payload.get("name") or ""

    try:
        default_due_date = normalize_due_date(args.due_date)
    except ValueError as e:
        sys.exit(f"Aborting: --due-date {args.due_date!r} is not a valid date: {e}")

    rows = read_rows(args.excel)
    if not rows:
        sys.exit("Input file has no data rows.")
    if not any("approval_request_id" in row for row in rows):
        sys.exit("[config] required column 'approval_request_id' not found in input file: "
                 f"{args.excel}. Every row is published by this id -- a blank/invalid cell just "
                 "skips that row, but the column itself must be present.")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Loaded {len(rows)} row(s) from {args.excel}. Mode: {mode}. "
                f"Batch size: {args.batch_size}. Retries: {args.max_retries}. published_by: {published_by}.")
    logger.info(f"Publish defaults: due_date={default_due_date} (cbp_plan_name is built at request level "
                f"from the request's own designation -- not read from the input row).")

    cfg = Config(
        execute=args.execute,
        cb_ext_course_base=cb_ext_course_service_url.rstrip("/"),
        user_token=user_token, published_by=published_by, approver_name=approver_name,
        user_id=args.user_id,
        default_due_date=default_due_date,
        max_retries=max(0, args.max_retries),
        backoff=max(0.0, args.retry_backoff),
        notify=enable_email_notification, notification_base=notification_base_url.rstrip("/"),
    )

    engine = create_async_engine(database_url, pool_size=max(5, args.batch_size + 2), max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    results: List[Optional[RowOutcome]] = [None] * len(rows)
    semaphore = asyncio.Semaphore(max(1, args.batch_size))
    progress = ProgressTracker(len(rows))

    try:
        async with httpx.AsyncClient(timeout=args.timeout) as client:
            async def worker(index, row):
                async with semaphore:
                    try:
                        res = await process_row(session_factory, client, row, cfg, progress)
                    except Exception as e:  # noqa: BLE001
                        logger.exception(f"Row {index} failed")
                        res = RowOutcome(
                            row_no=index, result=RowResult.FAILED, error=f"unexpected: {e}",
                            designation_name=_clean(row.get("designation_name")) or "",
                            approval_request_id=_clean(row.get("approval_request_id")) or "",
                            raw_row=row,
                        )
                    results[index - 1] = res

            await asyncio.gather(*(worker(i, row) for i, row in enumerate(rows, start=1)))
    finally:
        await engine.dispose()

    outcomes: List[RowOutcome] = [r for r in results if r is not None]

    approved = [r for r in outcomes if r.result == RowResult.APPROVED]
    already_approved = [r for r in outcomes if r.result == RowResult.ALREADY_APPROVED]
    would_approve = [r for r in outcomes if r.result == RowResult.WOULD_APPROVE]
    skipped_invalid = [r for r in outcomes if r.result == RowResult.SKIPPED_INVALID_ROW]
    skipped_not_pending = [r for r in outcomes if r.result == RowResult.SKIPPED_NOT_PENDING]
    failed = [r for r in outcomes if r.result not in _NON_FAILURE_STATUSES]

    logger.info("=" * 100)
    logger.info("RUN SUMMARY")
    logger.info("=" * 100)
    logger.info(f"Rows read:                    {len(rows)}")
    logger.info(f"Approved:                     {len(approved)}")
    logger.info(f"Already approved:             {len(already_approved)}")
    logger.info(f"Skipped (invalid input row):  {len(skipped_invalid)}")
    logger.info(f"Skipped (not pending):        {len(skipped_not_pending)}")
    logger.info(f"Failed:                       {len(failed)}")
    if mode == "DRY-RUN":
        logger.info(f"Would approve (dry-run):      {len(would_approve)}")

    if failed:
        logger.info("-" * 100)
        logger.info("FAILED DETAILS (also in the outcome CSV):")
        for r in failed:
            logger.info(f"  - row={r.row_no} approval_request_id={r.approval_request_id} "
                        f"status={r.result.value} error={r.error}")

    report_path = write_report_csv(outcomes, args.excel)
    logger.info("=" * 100)
    logger.info(f"Log file: {LOG_FILE}")
    logger.info(f"Outcome CSV written to: {report_path}")
    logger.info("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())

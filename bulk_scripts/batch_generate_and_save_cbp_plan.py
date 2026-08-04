"""
Standalone batch script: generate course recommendations and save CBP plans,
driven by an input file (.xlsx/.xlsm or .csv) whose mandatory `role_mapping_id` column
directly identifies each role_mapping to process (one data row == exactly one role_mapping /
one unit of work — there is no state/department scope expansion).

This script is FULLY SELF-CONTAINED. It does not import anything from `src/`
and does not call the HTTP API. All logic (role-mapping lookup, hybrid vector
search, LLM filtering, recommendation persistence, CBP plan persistence) is
re-implemented here directly against the database / Gemini, using the same
SQL/queries/prompts as:
    - src/api/v1/course_recommendation.py  (generate_course_recommendations, process_recommendation_task)
    - src/api/v1/cbp_plan.py               (save_cbp_plan)
    - src/crud/role_mapping.py, src/crud/course_recommendation.py, src/crud/cbp_plan.py
    - src/models/role_mapping.py, src/models/course_recommendation.py, src/models/cbp_plan.py

Input file (.xlsx/.xlsm or .csv) has 7 columns (header row 1):
    state_center_id, department_id, org_type, state_center_name, department_name,
    designation, role_mapping_id
Only `role_mapping_id` (a UUID) is mandatory as a column; the other 6 columns are carried
through purely so they can be echoed back into the outcome CSV. Every role_mapping_id in
the file is processed regardless of its DB status column.

Flow per input row (one role_mapping_id):
    Run the idempotency check (re-run-safe — this is what makes a second run of the script
    skip work already done):
        a. No recommendation row exists yet for this role_mapping
             -> generate the recommendation, then generate the CBP plan.
        b. A recommendation row exists but its status is FAILED (or a stale IN_PROGRESS from
           a crashed prior run)
             -> delete that recommendation row (and any CBP plan row) and retrigger generation
                from scratch.
        c. A recommendation row exists with status COMPLETED, but no CBP plan exists yet
             -> reuse the existing recommendation's filtered_courses; only generate the CBP plan
                (recommendation is NOT regenerated).
        d. A recommendation row exists with status COMPLETED AND a CBP plan already exists
             -> skip this role_mapping entirely.
    Before any fresh generation (cases a/b above), the role_mapping's igot_designation_name /
    igot_designation_id are checked: both are mandatory, so a role_mapping with neither set is
    skipped entirely (SKIPPED_NO_IGOT_DESIGNATION, no LLM call) -- in both dry-run and --execute.
       Recommendation generation (hybrid vector search + LLM filtering) writes
       status=IN_PROGRESS -> COMPLETED (or FAILED with error_message) directly into
       `recommended_courses`, exactly like the API's background task does. On success,
       `selected_courses` (mirrors save_cbp_plan's merge logic) is built from the
       recommendation's `filtered_courses` and inserted as a `cbp_plans` row.
    Any failure at any step for a given role_mapping is caught, logged, and the recommendation
    row is marked status=FAILED / error_message=<reason> (this IS the failure log — no separate
    log table is created). The script always continues to the next unit of work.

Configuration:
    - ALL environment variables are mandatory (no silent defaults); a missing one aborts the
      run with a clear RuntimeError (see require_env).
    - Command-line args: --excel (required) and --user-id (required UUID) are mandatory;
      --batch-size (default 10) sets the concurrency; dry-run is the DEFAULT and --execute
      opts into real database writes.

Concurrency: up to --batch-size (default 10) role_mappings are processed concurrently via an
asyncio.Semaphore. Retry: transient failures (DB/Gemini calls) are retried with exponential
backoff (see `with_retry`).

Logging: all logs go to a dedicated log file under bulk_scripts/logs/ (one file per run,
timestamped), in addition to the console. A run summary is printed and logged at the end,
and a per-role_mapping outcome CSV (input columns + recommendation_id + status +
total_courses + per-stage token counts + error) is written alongside it.

Usage:
    python bulk_scripts/batch_generate_and_save_cbp_plan.py \\
        --excel /path/to/input.xlsx --user-id <uuid> [--batch-size 10] [--execute]
"""

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from sqlalchemy import Column, Integer, String, Text, delete, desc, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from google import genai
from google.genai import types

# --------------------------------------------------------------------------------------
# Command-line arguments / script-level configuration
# --------------------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate course recommendations and save CBP plans for every role_mapping_id "
            "listed in an input file (.xlsx/.xlsm or .csv). Dry-run is the default; pass "
            "--execute to write to the database."
        )
    )
    parser.add_argument(
        "--excel",
        required=True,
        help="Path to the input file: .xlsx/.xlsm or .csv (must contain a 'role_mapping_id' column).",
    )
    parser.add_argument(
        "--user-id",
        required=True,
        type=uuid.UUID,
        help="Owner user_id (UUID) for all generated recommendations / cbp_plans.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Max number of role_mappings processed concurrently (default: 10).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt into real database writes. Without this flag the script runs in dry-run mode.",
    )
    return parser.parse_args()


args = parse_args()

EXCEL_FILE = args.excel
USER_ID = args.user_id
MAX_CONCURRENCY = args.batch_size

RETRY_ATTEMPTS = 3
RETRY_INITIAL_DELAY_SECONDS = 2.0
RETRY_EXP_BASE = 2.0

MIN_RELEVANCY = 80

# Dry-run is the DEFAULT; --execute opts into real DB writes. When dry-run is active, no rows
# are written to recommended_courses / cbp_plans, and the LLM/embedding pipeline is not called
# for role_mappings that would need fresh generation (reported as WOULD_GENERATE in the outcome
# CSV instead).
DRY_RUN = not args.execute

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
RUN_TIMESTAMP = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
LOG_FILE = os.path.join(LOG_DIR, f"batch_generate_and_save_cbp_plan_{RUN_TIMESTAMP}.log")

# Outcome CSV is written alongside the input Excel file (not under bulk_scripts/logs/), so it
# sits next to the file the user is already tracking for this batch.
OUTCOME_CSV_FILE = os.path.join(
    os.path.dirname(os.path.abspath(EXCEL_FILE)),
    f"batch_generate_and_save_cbp_plan_{RUN_TIMESTAMP}.csv",
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
    """Reads a mandatory environment variable. Raises RuntimeError if unset/empty — this
    script has no silent env-var defaults; every value must be provided explicitly."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. Set it in {ENV_FILE} or in "
            f"the environment before running this script."
        )
    return value


DATABASE_URL = require_env("DATABASE_URL")
GOOGLE_PROJECT_ID = require_env("GOOGLE_PROJECT_ID")
GOOGLE_PROJECT_LOCATION_GLOBAL = "global" or require_env("GOOGLE_PROJECT_LOCATION_GLOBAL")
GOOGLE_GENAI_USE_VERTEXAI = require_env("GOOGLE_GENAI_USE_VERTEXAI").strip().lower() == "true"
GOOGLE_APPLICATION_CREDENTIALS = require_env("GOOGLE_APPLICATION_CREDENTIALS")
GOOGLE_API_KEY = require_env("GOOGLE_API_KEY")
GOOGLE_EMBEDDING_MODEL = require_env("GOOGLE_EMBEDDING_MODEL")
EMBEDDING_OUTPUT_DIMENSIONALITY = int(require_env("EMBEDDING_OUTPUT_DIMENSIONALITY"))
GEMINI_PRO_MODEL_NAME = require_env("GEMINI_PRO_MODEL_NAME")

if not GOOGLE_APPLICATION_CREDENTIALS.startswith("/"):
    # configs.py resolves this relative to the repo root when run via uvicorn from there
    GOOGLE_APPLICATION_CREDENTIALS = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", GOOGLE_APPLICATION_CREDENTIALS
    )
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(GOOGLE_APPLICATION_CREDENTIALS)


# --------------------------------------------------------------------------------------
# Logging setup (console + dedicated file)
# --------------------------------------------------------------------------------------

logger = logging.getLogger("batch_cbp_plan")
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
# Prompts (copied from src/prompts/prompts.py so the script has zero src/ imports)
# --------------------------------------------------------------------------------------

VECTOR_QUERY_SYSTEM_PROMPT = """You are an expert learning & development advisor for civil servants.
Given a detailed role profile, generate three distinct search queries and a keyword list for
retrieving training courses. All outputs must be specific, rich in domain terminology, non-generic.

Return ONLY a JSON object with these exact keys:
- keyword_query: A compact phrase (15-30 words) of role-specific skills, tools, and domain keywords.
  Focus on technical/functional skills and sector-specific terminology.
- description_query: A narrative paragraph (60-100 words) describing what this role does, the challenges
  it faces, and what knowledge gaps need to be filled. Include sector and ministry context.
- combined_query: A rich multi-angle query (80-120 words) covering domain knowledge, functional
  competencies, behavioral competencies, sector-specific regulations/policies, and desired learning outcomes.
  Emphasise the specific government sector (e.g. health, finance, urban development, defence).
- search_keywords: An array of 10-15 individual domain/skill/topic words or short phrases (2-3 words max each)
  extracted from the role. These will be used for Postgres full-text and array keyword search.
  Include sector-specific terms, competency area names, tools, policies, and skill topics.
  NO generic words like "management", "leadership", "communication" unless they are genuinely specific to the role.

Do NOT return markdown. Return raw JSON only."""

COURSE_SELECTION_SYSTEM_PROMPT = """
You are a senior Learning & Development advisor for government civil servants.

Your task:
Analyze the candidate courses provided, select the best 50-60 courses and provide a relevancy percentage for each, indicating how relevant each course is for the given role (Designation) profile.

## Selection Rules

### 1. Contextual Role (Designation) Analysis (Mandatory)

Before evaluating any course, first analyse the complete Role (Designation) profile to understand the designation's purpose, expected responsibilities, decision-making authority, operational scope, nature of work, and expected outcomes. Identify the Domain, Functional and Behavioral learning needs based on the role context before ranking courses.

Never recommend or rank courses solely based on competency names or course titles similarity.

---

### 2. Relevancy Scoring

Provide a relevancy percentage based on holistic contextual analysis rather than keywords matching alone.

While assigning relevancy, the Course Description must be used for analysing the course context, keywords, name, scope and applicability to the learner's role, as it provides the richest contextual information.

Analyse the following inputs in order of importance:

- Course Description (Highest Priority)
- Course Keywords / Metadata
- Sector Alignment
- Ministry/Department Alignment
- Own Organisation Alignment
- Designation Context
- Role Responsibilities (R&R)
- Competency Alignment
- Policy / Programme / Governance Context

Course titles should only be used as supporting evidence and must never be the primary reason for assigning a high relevancy percentage. If the course title and course description differ in specificity, always prioritise the course description while evaluating relevance.

---

### 3. Contextual Re-ranking

Re-rank all candidate courses after analysing the complete role profile.

Ranking must be aligned with:

- Designation
- Role Seniority
- Nature of Responsibilities
- Sector
- Ministry/Department
- own Organisation
- Competency Requirements
- Government Policy / Programme Context

---

### 4. Provider Priority

Prefer courses from the user's own organisation (Own Org: YES) only when they are contextually relevant to the learner's role. Fill remaining slots by relevance score.

Being from the same organisation alone must never justify recommending an irrelevant course.

When multiple courses have similar contextual relevance, prioritize them in the following order:

- Own Organisation
- Same Ministry
- Same Sector
- Other Providers

---

### 5. Competency Mix

#### Domain

- Domain courses must be directly aligned with the Role (Designation) sector, ministry, department, policies, schemes, programmes and technical work area.
- Domain courses must support the actual responsibilities of the designation and not merely the organisation name.
- Generic leadership, management or communication courses must NEVER be classified as Domain courses.

#### Behavioral

- Analyse the behavioural expectations of the designation before ranking/recommending behavioral courses.
- Consider factors such as leadership responsibility, citizen interaction, communication needs, ethics, integrity, teamwork, conflict management, emotional intelligence, supervision and decision-making responsibilities based on Role (Designation) nature.

#### Functional

- Analyse the designation's Roles & Responsibilities (R&R) and operational nature before recommending functional courses.
- Recommend and rank functional courses that directly improve day-to-day execution of the learner's responsibilities.
- Identify whether the role requires competencies such as finance, procurement, project management, administration, digital governance, policy drafting, legal processes, HR, monitoring & evaluation, office procedures or data analysis.

---

### 6. Domain Diversity

- Domain courses must NOT all cover the same topic or be from one provider.
- Include at least 3-4 distinct domain sub-topics wherever applicable.
- Do not recommend more than TWO courses covering substantially the same topic unless indication or contexually provide enriched learning outcomes.

---

### 7. "Know Your Ministry/Department" Course Rule (Mandatory)

A course titled or categorized as "Know Your Ministry" or "Know Your Department" must ONLY be included if it belongs to the SAME ministry and department as the learner's role profile.

- If the course ministry/department exactly matches the learner's ministry/department, evaluate it normally.
- If it belongs to a different ministry or department, DISCARD it regardless of relevancy score.

Never infer similarity between ministries or departments. Exact contextual matching is mandatory.

---

### 8. Duplicate Course Handling

Avoid recommending multiple courses with nearly identical learning outcomes/description.

If similar courses exist:

- Compare them contextually.
- Recommend only the best aligned course(s).
- Do not recommend more than TWO/three courses covering essentially the same topic.

---

### 10. Sort Output

Sort recommendations in the following order:

- Own Organisation Domain Courses
- Own Organisation Functional Courses
- Own Organisation Behavioral Courses
- Remaining Domain Courses
- Remaining Functional Courses
- Remaining Behavioral Courses

Within each category, sort by:

- Higher contextual relevancy
- Better designation fit
- Better responsibility alignment
- Better competency alignment

---

### 11. Language Preference & Sorting
(should influence ranking only after contextual relevance has been established. Never recommend a less relevant course solely because it is available in a preferred language.)

Select and rank courses based on the learner's administrative context and preferred working language, while ensuring the course remains contextually relevant to the role.

#### For State Government roles

- Prefer courses available in the official language(s) of the respective state wherever available.
- If equivalent courses exist in both English and the state's official language, prioritize the state language version for operational and field-level roles.
- If a suitable state language course is unavailable, recommend the English version.

#### For Central Government organisation roles

- Prefer English courses for strategic, policy-making, leadership and senior management roles (e.g., Director, Joint Secretary, Additional Secretary, Secretary, etc.), as these roles primarily operate in English.
- For operational, field-level and implementation-focused roles, prioritize Hindi language courses where they improve accessibility and practical learning, while considering the learner's organisation and context.
- If multiple language versions of the same course exist, recommend only the most appropriate language version and avoid recommending duplicate courses in different languages unless there is a strong contextual requirement.

Return ONLY a JSON array. No markdown.
"""


# --------------------------------------------------------------------------------------
# SQLAlchemy models (minimal standalone re-declaration matching src/models/*)
# --------------------------------------------------------------------------------------

Base = declarative_base()


class ProcessingStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RecommendationStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RoleMapping(Base):
    __tablename__ = "role_mappings"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    state_center_id = Column(String(32), nullable=False)
    department_id = Column(String(32), nullable=True)
    state_center_name = Column(String(255), nullable=True)
    department_name = Column(String(255), nullable=True)
    status = Column(String(50), nullable=True)
    sector_name = Column(String(255), nullable=True)
    designation_name = Column(String(255), nullable=True)
    wing_division_section = Column(String(255), nullable=True)
    role_responsibilities = Column(JSONB, nullable=True)
    activities = Column(JSONB, nullable=True)
    competencies = Column(JSONB, nullable=True)
    sort_order = Column(Integer, nullable=True)
    igot_designation_name = Column(String, nullable=True)
    igot_designation_id = Column(String, nullable=True)


class RecommendedCourse(Base):
    __tablename__ = "recommended_courses"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    role_mapping_id = Column(PG_UUID(as_uuid=True), nullable=False)
    status = Column(String, nullable=False)
    error_message = Column(Text, nullable=True)
    vector_query = Column(Text, nullable=True)
    embedding = Column(JSONB, nullable=True)
    actual_courses = Column(JSONB, nullable=True)
    filtered_courses = Column(JSONB, nullable=True)


class CBPPlan(Base):
    __tablename__ = "cbp_plans"

    id = Column(PG_UUID(as_uuid=True), primary_key=True)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False)
    role_mapping_id = Column(PG_UUID(as_uuid=True), nullable=False)
    recommended_course_id = Column(PG_UUID(as_uuid=True), nullable=True)
    selected_courses = Column(JSONB, nullable=False)


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
            # Suppress any secondary failure during rollback/close: under concurrent pool
            # pressure the underlying connection may already be dead, and rollback/close on a
            # dead connection would raise its own InterfaceError that would otherwise mask (or
            # escape past) the original error and break process_role_mapping's error handling.
            with contextlib.suppress(Exception):
                await session.rollback()
            raise
        finally:
            with contextlib.suppress(Exception):
                await session.close()


# --------------------------------------------------------------------------------------
# Gemini clients (mirrors src/api/v1/course_recommendation.py)
# --------------------------------------------------------------------------------------

gemini_client = genai.Client(
    project=GOOGLE_PROJECT_ID,
    location=GOOGLE_PROJECT_LOCATION_GLOBAL,
    vertexai=GOOGLE_GENAI_USE_VERTEXAI,
)

embedding_client = genai.Client(
    api_key=GOOGLE_API_KEY,
    vertexai=False,
)


# --------------------------------------------------------------------------------------
# Retry helper
# --------------------------------------------------------------------------------------

async def with_retry(coro_fn, *args, description: str = "operation", **kwargs):
    """
    Calls coro_fn(*args, **kwargs) with exponential-backoff retry.
    Raises the last exception if all attempts are exhausted.
    Logs the wall-clock time taken by each attempt at INFO level.
    """
    delay = RETRY_INITIAL_DELAY_SECONDS
    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            result = await coro_fn(*args, **kwargs)
            elapsed = time.perf_counter() - start
            logger.info(f"  [timing] {description} took {elapsed:.3f}s")
            return result
        except Exception as e:
            elapsed = time.perf_counter() - start
            logger.info(f"  [timing] {description} failed after {elapsed:.3f}s")
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


@dataclass
class SkippedRow:
    """A data row dropped by read_excel_rows because role_mapping_id was blank or not a
    valid UUID -- kept (with all its other input columns, same as ExcelRow) so it can
    still be reported as its own, fully-populated row in the outcome CSV (previously it
    only affected the skipped_rows count in a log line, with no trace in the CSV)."""
    row_number: int
    state_center_id: str
    department_id: str
    org_type: str
    state_center_name: str
    department_name: str
    designation: str
    raw_role_mapping_id: str
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
    Reads the input file (.xlsx/.xlsm or .csv). The ONLY mandatory column is 'role_mapping_id' (a
    UUID that directly identifies the role_mapping to process). The other 6 columns
    (state_center_id, department_id, org_type, state_center_name, department_name, designation)
    are optional and carried through purely so they can be echoed into the outcome CSV.

    A data row with a blank/invalid role_mapping_id is dropped from the returned ExcelRow list
    (logged as a warning), but is still returned in the second list (SkippedRow) so it shows up
    as its own row in the outcome CSV instead of only affecting a count in the log.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Input file not found: {path}")

    header_list, data_rows = _iter_input_rows(path)
    headers: Dict[str, int] = {h: i for i, h in enumerate(header_list) if h}

    if headers.get("role_mapping_id") is None:
        raise SystemExit(
            f"[config] required column 'role_mapping_id' not found in input file: {path}. "
            f"Headers found: {list(headers.keys())}"
        )

    rm_col = headers["role_mapping_id"]

    def read_text_col(row, name: str) -> str:
        col = headers.get(name)
        if col is None or col >= len(row):
            return ""
        val = row[col]
        if val in (None, ""):
            return ""
        return str(val).strip()

    rows: List[ExcelRow] = []
    skipped_rows: List[SkippedRow] = []
    for row_idx, row in enumerate(data_rows, start=2):
        if not row or all(v is None or str(v).strip() == "" for v in row):
            continue
        rm_val = row[rm_col] if rm_col < len(row) else None

        skipped_row_kwargs = dict(
            row_number=row_idx,
            state_center_id=read_text_col(row, "state_center_id"),
            department_id=read_text_col(row, "department_id"),
            org_type=read_text_col(row, "org_type"),
            state_center_name=read_text_col(row, "state_center_name"),
            department_name=read_text_col(row, "department_name"),
            designation=read_text_col(row, "designation"),
        )

        if rm_val in (None, ""):
            reason = "missing role_mapping_id"
            logger.warning(f"Row {row_idx}: {reason} -> skipping row")
            skipped_rows.append(SkippedRow(**skipped_row_kwargs, raw_role_mapping_id="", reason=reason))
            continue

        try:
            role_mapping_id = uuid.UUID(str(rm_val).strip())
        except ValueError:
            reason = f"role_mapping_id {rm_val!r} is not a valid UUID"
            logger.warning(f"Row {row_idx}: {reason} -> skipping row")
            skipped_rows.append(SkippedRow(**skipped_row_kwargs, raw_role_mapping_id=str(rm_val), reason=reason))
            continue

        rows.append(
            ExcelRow(
                row_number=row_idx,
                state_center_id=read_text_col(row, "state_center_id"),
                department_id=read_text_col(row, "department_id"),
                org_type=read_text_col(row, "org_type"),
                state_center_name=read_text_col(row, "state_center_name"),
                department_name=read_text_col(row, "department_name"),
                designation=read_text_col(row, "designation"),
                role_mapping_id=role_mapping_id,
            )
        )

    logger.info(
        f"Read {len(rows)} valid data row(s) from input file ({len(skipped_rows)} skipped due to "
        f"missing/invalid role_mapping_id): {path}"
    )
    return rows, skipped_rows


# --------------------------------------------------------------------------------------
# Role mapping lookup
# --------------------------------------------------------------------------------------

async def fetch_role_mapping_by_id(db: AsyncSession, role_mapping_id: uuid.UUID) -> Optional[RoleMapping]:
    """Fetches the full role_mapping row (all fields needed to build the user profile)."""
    stmt = select(RoleMapping).where(RoleMapping.id == role_mapping_id)
    result = await db.execute(stmt)
    return result.scalars().first()


# --------------------------------------------------------------------------------------
# Idempotency: existing recommendation / cbp_plan lookup + cleanup
# --------------------------------------------------------------------------------------

async def get_existing_recommendation(db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[RecommendedCourse]:
    stmt = select(RecommendedCourse).filter(
        RecommendedCourse.role_mapping_id == role_mapping_id,
        RecommendedCourse.user_id == user_id,
    ).limit(1)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_existing_cbp_plan(db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> Optional[CBPPlan]:
    stmt = (
        select(CBPPlan)
        .filter(CBPPlan.role_mapping_id == role_mapping_id, CBPPlan.user_id == user_id)
        .order_by(desc(CBPPlan.id))
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def delete_existing_records(db: AsyncSession, role_mapping_id: uuid.UUID, user_id: uuid.UUID) -> None:
    await db.execute(
        delete(CBPPlan).where(CBPPlan.role_mapping_id == role_mapping_id, CBPPlan.user_id == user_id)
    )
    await db.execute(
        delete(RecommendedCourse).where(
            RecommendedCourse.role_mapping_id == role_mapping_id, RecommendedCourse.user_id == user_id
        )
    )
    await db.commit()


# --------------------------------------------------------------------------------------
# Course recommendation generation (mirrors process_recommendation_task)
# --------------------------------------------------------------------------------------

@dataclass
class TokenTally:
    """Accumulates per-LLM-stage usage for one role_mapping. A single instance is threaded
    through the generation pipeline and mutated by the inner functions ONLY right before they
    return successfully, so retried (failed) attempts never contribute — only the successful
    final attempt of each call is counted.

    NOTE: contextual_queries_usage and filter_courses_usage hold the COMPLETE Gemini
    generate_content usage_metadata object (prompt_token_count, candidates_token_count,
    thoughts_token_count, total_token_count, etc.), not just a single number. embedding_tokens
    stays a plain int: the embedding stage's billable CHARACTER count (embeddings are billed
    by characters, not tokens, and carry no comparable richer usage object).

    pgvector_courses_count is the size of the deduped candidate pool returned by the
    hybrid/keyword/competency searches BEFORE the LLM filtering step (i.e. len(all_candidates)
    in generate_recommendation_for_role_mapping) — distinct from total_courses on UnitOutcome,
    which is the count AFTER LLM filtering (len(final_filtered_courses))."""

    contextual_queries_usage: Optional[Dict[str, Any]] = None
    filter_courses_usage: Optional[Dict[str, Any]] = None
    embedding_tokens: int = 0
    pgvector_courses_count: int = 0

    @property
    def total_tokens(self) -> int:
        contextual = (self.contextual_queries_usage or {}).get("total_token_count", 0) or 0
        filter_ = (self.filter_courses_usage or {}).get("total_token_count", 0) or 0
        return int(contextual) + int(filter_) + self.embedding_tokens


def _log_generate_usage(call_name: str, response) -> Optional[Dict[str, Any]]:
    """Logs the complete Gemini generate_content usage_metadata object as JSON and returns
    that same dict (None if the response carries no usage_metadata)."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        logger.info(f"      [usage] {call_name}: no usage_metadata in response")
        return None
    usage_json = usage.model_dump(mode="json", exclude_none=True)
    logger.info(f"      [usage] {call_name}: {json.dumps(usage_json, ensure_ascii=False)}")
    return usage_json


def _log_embed_usage(call_name: str, response) -> int:
    """Logs Gemini embed_content usage and returns a token-count for the embedding tally.
    Embedding responses are billed by CHARACTERS, not tokens, and usually carry no
    token_count — so we return the billable_character_count as the embedding-stage figure
    (falling back to usage_metadata.total_token_count if present, else 0)."""
    metadata = getattr(response, "metadata", None)
    billable_chars = getattr(metadata, "billable_character_count", None) if metadata else None
    logger.info(f"      [usage] {call_name}: billable_character_count={billable_chars}")
    if billable_chars:
        return int(billable_chars)
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        return int(getattr(usage, "total_token_count", None) or 0)
    return 0


async def get_embedding(text_input: str, tally: Optional["TokenTally"] = None) -> list:
    if not text_input.strip():
        return []
    vector_query = f"task: search result | query: {text_input}"
    response = await embedding_client.aio.models.embed_content(
        model=GOOGLE_EMBEDDING_MODEL,
        contents=vector_query,
        config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_OUTPUT_DIMENSIONALITY),
    )
    tokens = _log_embed_usage("get_embedding", response)
    # Add to the tally only right before a successful return, so retried attempts don't double-count.
    if tally is not None:
        tally.embedding_tokens += tokens
    return response.embeddings


async def generate_contextual_queries(user_profile: str, tally: Optional["TokenTally"] = None) -> Dict[str, Any]:
    user_part = types.Part.from_text(text=f"Role Profile:\n{user_profile}")
    contents = [types.Content(role="user", parts=[user_part])]

    config = types.GenerateContentConfig(
        temperature=0.4,
        top_p=0.95,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema={
            "type": "OBJECT",
            "properties": {
                "keyword_query": {"type": "STRING"},
                "description_query": {"type": "STRING"},
                "combined_query": {"type": "STRING"},
                "search_keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["keyword_query", "description_query", "combined_query", "search_keywords"],
        },
        system_instruction=[types.Part.from_text(text=VECTOR_QUERY_SYSTEM_PROMPT)],
    )

    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_PRO_MODEL_NAME,
        contents=contents,
        config=config,
    )
    usage = _log_generate_usage("generate_contextual_queries", response)
    if not response.text:
        raise Exception("generate_contextual_queries: LLM returned empty response")
    result = json.loads(response.text)
    # Set on the tally only right before a successful return, so retried attempts don't double-count.
    if tally is not None:
        tally.contextual_queries_usage = usage
    return result


async def get_filtered_courses_by_llm(
    courses_prompt: str, user_profile: str, organisation: str, tally: Optional["TokenTally"] = None
) -> str:
    user_part = types.Part.from_text(
        text=f"""
Role Profile:
{user_profile}

Own Organisation: {organisation or 'N/A'}

Candidate Courses:
{courses_prompt}
"""
    )

    response_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "identifier": {"type": "STRING"},
                "course": {"type": "STRING"},
                "relevancy": {"type": "INTEGER"},
                "rationale": {"type": "STRING"},
            },
            "required": ["identifier", "course", "relevancy", "rationale"],
        },
    }

    config = types.GenerateContentConfig(
        temperature=0,
        top_p=1,
        safety_settings=[
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="OFF"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="OFF"),
        ],
        response_mime_type="application/json",
        response_schema=response_schema,
        system_instruction=[types.Part.from_text(text=COURSE_SELECTION_SYSTEM_PROMPT)],
        thinking_config=types.ThinkingConfig(include_thoughts=False, thinking_budget=2048),
    )

    response = await gemini_client.aio.models.generate_content(
        model=GEMINI_PRO_MODEL_NAME,
        contents=[types.Content(role="user", parts=[user_part])],
        config=config,
    )
    usage = _log_generate_usage("get_filtered_courses_by_llm", response)
    # Set on the tally only right before a successful return, so retried attempts don't double-count.
    if tally is not None:
        tally.filter_courses_usage = usage
    if not response.text:
        return "[]"
    return response.text


def _build_competency_query(competencies: list) -> str:
    if not competencies:
        return ""
    parts = []
    for c in competencies:
        area = c.get("competencyAreaName") or c.get("type") or ""
        theme = c.get("competencyThemeName") or c.get("theme") or ""
        sub = c.get("competencySubThemeName") or c.get("sub_theme") or ""
        if area or theme:
            parts.append(f"Type: {area} -> Theme: {theme} -> Sub-Theme: {sub}")
    if not parts:
        return ""
    return "Training course covering the following government competencies: " + " | ".join(parts)


def _build_competency_query_by_type(competencies: list, competency_type: str) -> str:
    if not competencies:
        return ""
    filtered = [
        c for c in competencies
        if competency_type in (c.get("competencyAreaName") or c.get("type") or "").lower()
    ]
    return _build_competency_query(filtered)


async def fetch_hybrid_search_courses(db: AsyncSession, keyword_emb, description_emb, combined_emb, limit: int = 100):
    sql_query = text(f"""
        SELECT
            identifier,
            name,
            (
                0.40 * (1.0 - (keywords_embedding    <=> '{keyword_emb}')) +
                0.20 * (1.0 - (description_embedding <=> '{description_emb}')) +
                0.40 * (1.0 - (combined_embedding    <=> '{combined_emb}'))
            ) AS weighted_score
        FROM public.course_metadata_weightage
        ORDER BY weighted_score DESC
        LIMIT {limit};
    """)
    result = await db.execute(sql_query)
    return result.all()


async def fetch_keyword_search_courses(db: AsyncSession, keywords: List[str], limit: int = 40):
    if not keywords:
        return []

    array_overlaps = " OR ".join(f"keywords && ARRAY[:{f'kw{i}'}]" for i, _ in enumerate(keywords))
    name_ilike = " OR ".join(f"name ILIKE :{f'nl{i}'}" for i, _ in enumerate(keywords))
    fts_parts = " OR ".join(
        f"description_tsv @@ plainto_tsquery('english', :{f'fts{i}'})"
        for i, _ in enumerate(keywords)
    )

    params: Dict[str, Any] = {}
    for i, kw in enumerate(keywords):
        params[f"kw{i}"] = kw
        params[f"nl{i}"] = f"%{kw}%"
        params[f"fts{i}"] = kw

    sql = text(f"""
        SELECT
            identifier,
            name,
            (
                CASE WHEN ({array_overlaps}) THEN 1 ELSE 0 END +
                CASE WHEN ({name_ilike})     THEN 1 ELSE 0 END +
                CASE WHEN ({fts_parts})       THEN 1 ELSE 0 END
            )::float AS keyword_score
        FROM public.course_metadata_weightage
        WHERE
            ({array_overlaps})
            OR ({name_ilike})
            OR ({fts_parts})
        ORDER BY keyword_score DESC
        LIMIT {limit};
    """)
    result = await db.execute(sql, params)
    return result.all()


async def fetch_competency_typed_courses(db: AsyncSession, combined_emb, competency_type: str, limit: int = 40):
    sql_query = text(f"""
        SELECT
            identifier,
            name,
            (1.0 - (combined_embedding <=> '{combined_emb}')) AS score
        FROM public.course_metadata_weightage
        WHERE EXISTS (
            SELECT 1
            FROM jsonb_array_elements(competencies_v6) AS comp
            WHERE lower(comp->>'competencyAreaName') LIKE '%{competency_type}%'
        )
        ORDER BY score DESC
        LIMIT {limit};
    """)
    result = await db.execute(sql_query)
    return result.all()


async def fetch_course_metadata(db: AsyncSession, identifiers_str: str):
    sql_query = text(f"""
        SELECT identifier, competencies_v6, duration, organisation, keywords, description, name
        FROM public.course_metadata_weightage
        WHERE identifier IN ({identifiers_str});
    """)
    result = await db.execute(sql_query)
    return result.all()


async def generate_recommendation_for_role_mapping(
    recommendation_id: uuid.UUID,
    user_profile: str,
    ministry_state_name: str,
    department_name: str,
    raw_competencies: Optional[list],
    tally: "TokenTally",
) -> Tuple[str, List[Dict[str, Any]], List[float], List[Dict[str, Any]]]:
    """
    Runs the full generation pipeline (queries -> embeddings -> hybrid search -> LLM filter
    -> enrichment) and returns (vector_query_json, all_candidates, kw_emb, final_filtered_courses).
    Does NOT persist; caller persists via update_recommendation_completed/failed.

    Accumulates per-stage LLM usage into the passed-in `tally` (mutated in place).
    """
    queries = await with_retry(generate_contextual_queries, user_profile, tally=tally, description="generate_contextual_queries")
    keyword_query = queries.get("keyword_query", "")
    description_query = queries.get("description_query", "")
    combined_query = queries.get("combined_query", "")
    search_keywords = queries.get("search_keywords", [])

    functional_competency_query = _build_competency_query_by_type(raw_competencies or [], "functional")
    behavioural_competency_query = _build_competency_query_by_type(raw_competencies or [], "behavioral")

    all_queries = [
        {
            "keyword_query": keyword_query,
            "description_query": description_query,
            "combined_query": combined_query,
            "search_keywords": search_keywords,
            "functional_competency_query": functional_competency_query,
            "behavioural_competency_query": behavioural_competency_query,
        }
    ]

    kw_emb_list, desc_emb_list, comb_emb_list, func_comp_emb_list, behav_comp_emb_list = await asyncio.gather(
        with_retry(get_embedding, keyword_query, tally=tally, description="embed keyword_query"),
        with_retry(get_embedding, description_query, tally=tally, description="embed description_query"),
        with_retry(get_embedding, combined_query, tally=tally, description="embed combined_query"),
        with_retry(get_embedding, functional_competency_query, tally=tally, description="embed functional_competency_query"),
        with_retry(get_embedding, behavioural_competency_query, tally=tally, description="embed behavioural_competency_query"),
    )
    if not kw_emb_list or not desc_emb_list or not comb_emb_list:
        raise Exception("Failed to generate one or more embeddings")

    kw_emb = kw_emb_list[0].values
    desc_emb = desc_emb_list[0].values
    comb_emb = comb_emb_list[0].values
    func_comp_emb = func_comp_emb_list[0].values if func_comp_emb_list else None
    behav_comp_emb = behav_comp_emb_list[0].values if behav_comp_emb_list else None

    # Each concurrent query gets its OWN session — a single AsyncSession cannot be shared
    # across concurrently-awaited operations (asyncio.gather here runs 4 queries in parallel;
    # SQLAlchemy raises "session is provisioning a new connection; concurrent operations are
    # not permitted" if they share one session).
    async def _query_with_own_session(fn, *args, **kwargs):
        async with get_session() as session:
            return await with_retry(fn, session, *args, **kwargs)

    vector_results, kw_results, func_results, behav_results = await asyncio.gather(
        _query_with_own_session(fetch_hybrid_search_courses, kw_emb, desc_emb, comb_emb, limit=100, description="fetch_hybrid_search_courses"),
        _query_with_own_session(fetch_keyword_search_courses, search_keywords, limit=40, description="fetch_keyword_search_courses"),
        _query_with_own_session(fetch_competency_typed_courses, func_comp_emb or comb_emb, "functional", limit=40, description="fetch_competency_typed_courses(functional)"),
        _query_with_own_session(fetch_competency_typed_courses, behav_comp_emb or comb_emb, "behavioural", limit=40, description="fetch_competency_typed_courses(behavioural)"),
    )

    async with get_session() as db:

        seen: Dict[str, Dict[str, Any]] = {}
        for identifier, name, score in vector_results:
            seen[identifier] = {"identifier": identifier, "name": name, "distance": float(score)}

        for identifier, name, kw_score in kw_results:
            bonus = min(float(kw_score) / 3.0, 1.0) * 0.15
            if identifier in seen:
                seen[identifier]["distance"] = seen[identifier]["distance"] + bonus
            else:
                seen[identifier] = {"identifier": identifier, "name": name, "distance": bonus}

        for identifier, name, score in (func_results or []) + (behav_results or []):
            if identifier in seen:
                seen[identifier]["distance"] = max(seen[identifier]["distance"], float(score)) + 0.10
            else:
                seen[identifier] = {"identifier": identifier, "name": name, "distance": float(score) + 0.10}

        all_candidates = sorted(seen.values(), key=lambda c: c["distance"], reverse=True)
        tally.pgvector_courses_count = len(all_candidates)

        all_identifiers = [c["identifier"] for c in all_candidates]
        if all_identifiers:
            identifiers_str = ", ".join(f"'{i}'" for i in all_identifiers)
            metadata_rows = await with_retry(fetch_course_metadata, db, identifiers_str, description="fetch_course_metadata(candidates)")
            metadata_map = {row.identifier: row for row in metadata_rows}
        else:
            metadata_map = {}

        candidate_lines = []
        for c in all_candidates:
            meta = metadata_map.get(c["identifier"])
            org_raw = getattr(meta, "organisation", None)
            if isinstance(org_raw, list):
                org_info = ", ".join(str(o) for o in org_raw if o)
            else:
                org_info = str(org_raw) if org_raw else ""
            c["competencies"] = getattr(meta, "competencies_v6", None)
            is_own_org = "YES" if (ministry_state_name and org_info and ministry_state_name.lower() in org_info.lower()) else "NO"
            if is_own_org == "NO" and department_name and org_info and department_name.lower() in org_info.lower():
                is_own_org = "YES"

            candidate_lines.append(
                f"Course ID: {c['identifier']} | "
                f"Course Name: {c['name']} | "
                f"Course Description: {getattr(meta, 'description', None)} | "
                f"Course Keywords: {getattr(meta, 'keywords', None)} | "
                f"Similarity: {c['distance']:.4f} | "
                f"Organisation: {org_info or 'N/A'} | "
                f"Own Org: {is_own_org} | "
            )

        courses_prompt = "\n".join(candidate_lines)

        filtered_courses_json = await with_retry(
            get_filtered_courses_by_llm,
            courses_prompt,
            user_profile,
            department_name or ministry_state_name,
            tally=tally,
            description="get_filtered_courses_by_llm",
        )
        filtered_courses = json.loads(filtered_courses_json)

        filtered_identifiers = [c["identifier"] for c in filtered_courses]
        if filtered_identifiers:
            f_identifiers_str = ", ".join(f"'{i}'" for i in filtered_identifiers)
            enriched_rows = await with_retry(fetch_course_metadata, db, f_identifiers_str, description="fetch_course_metadata(filtered)")
            enriched_map = {row.identifier: row for row in enriched_rows}
        else:
            enriched_map = {}

    filtered_courses = [c for c in filtered_courses if c["identifier"] in enriched_map]
    for course in filtered_courses:
        course["is_public"] = False
        meta = enriched_map.get(course["identifier"])
        if meta:
            course["course"] = meta.name
            course["competencies"] = meta.competencies_v6
            course["duration"] = meta.duration
            org = meta.organisation
            course["organisation"] = ", ".join(str(o) for o in org if o) if isinstance(org, list) else (org or None)

    above_threshold = [c for c in filtered_courses if c.get("relevancy", 0) >= MIN_RELEVANCY]

    # Split by competency category (Domain / Functional / Behavioural) and cap each bucket,
    # ranked by relevancy within the bucket. A course is Functional/Behavioural if ANY of its
    # competencies_v6 entries match that type (same substring convention as
    # _build_competency_query_by_type); everything else defaults to Domain.
    def _course_category(course: Dict[str, Any]) -> str:
        for comp in (course.get("competencies") or []):
            area = (comp.get("competencyAreaName") or comp.get("type") or "").lower()
            if "functional" in area:
                return "functional"
            if "behavioral" in area or "behavioural" in area:
                return "behavioural"
        return "domain"

    CATEGORY_CAPS = {"domain": 8, "functional": 4, "behavioural": 4}
    buckets: Dict[str, List[Dict[str, Any]]] = {"domain": [], "functional": [], "behavioural": []}
    for c in above_threshold:
        buckets[_course_category(c)].append(c)

    final_filtered_courses: List[Dict[str, Any]] = []
    for category, cap in CATEGORY_CAPS.items():
        bucket = sorted(buckets[category], key=lambda c: c.get("relevancy", 0), reverse=True)
        final_filtered_courses.extend(bucket[:cap])

    final_filtered_courses.sort(key=lambda c: c.get("relevancy", 0), reverse=True)

    return json.dumps(all_queries, ensure_ascii=False), all_candidates, kw_emb, final_filtered_courses


# --------------------------------------------------------------------------------------
# CBP plan creation (mirrors save_cbp_plan, minus the KB search_courses + user_added_course
# fallback paths, since every course_identifier here comes directly from filtered_courses)
# --------------------------------------------------------------------------------------

def convert_for_json(data_list):
    for item in data_list:
        for k, v in item.items():
            if isinstance(v, uuid.UUID):
                item[k] = str(v)
            elif isinstance(v, datetime):
                item[k] = v.isoformat()
    return data_list


async def create_cbp_plan(
    db: AsyncSession,
    user_id: uuid.UUID,
    role_mapping_id: uuid.UUID,
    recommended_course_id: uuid.UUID,
    selected_courses: List[Dict[str, Any]],
) -> CBPPlan:
    db_obj = CBPPlan(
        id=uuid.uuid4(),
        user_id=user_id,
        role_mapping_id=role_mapping_id,
        recommended_course_id=recommended_course_id,
        selected_courses=convert_for_json(selected_courses),
    )
    db.add(db_obj)
    await db.commit()
    await db.refresh(db_obj)
    return db_obj


# --------------------------------------------------------------------------------------
# Per-role-mapping unit of work
# --------------------------------------------------------------------------------------

class UnitResult(str, Enum):
    SKIPPED_EXISTING = "SKIPPED_EXISTING"
    SKIPPED_NO_IGOT_DESIGNATION = "SKIPPED_NO_IGOT_DESIGNATION"  # role_mapping has neither
    # igot_designation_name nor igot_designation_id -- both are mandatory, so no course
    # recommendation is generated for it.
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WOULD_GENERATE = "WOULD_GENERATE"  # dry-run only: no existing COMPLETED recommendation+plan,
    # but generation was NOT actually run (no LLM calls) -- this row reports the plan only.


@dataclass
class UnitOutcome:
    excel_row: ExcelRow            # holds all 7 input columns + row_number
    result: UnitResult
    recommendation_id: Optional[uuid.UUID] = None
    total_courses: int = 0
    tokens: Optional[TokenTally] = None
    error: Optional[str] = None


def _build_user_profile(role_mapping: RoleMapping) -> str:
    """Mirrors the user_profile text built in generate_course_recommendations endpoint."""
    competencies_json = json.dumps(role_mapping.competencies, indent=2) if role_mapping.competencies else "[]"
    return f"""
Ministry/State/Organisation: {role_mapping.state_center_name}
Department Name: {role_mapping.department_name if role_mapping.department_name else 'N/A'}
Sector: {role_mapping.sector_name if role_mapping.sector_name else 'N/A'}
Designation Name: {role_mapping.designation_name}
Wing/Division/Section: {role_mapping.wing_division_section if role_mapping.wing_division_section else 'N/A'}
Roles & Responsibilities: {role_mapping.role_responsibilities}
Key Activities: {role_mapping.activities}
Competencies (with definitions):
{competencies_json}
"""


async def run_recommendation_generation(
    role_mapping: RoleMapping, recommendation_id: uuid.UUID, label: str, tally: "TokenTally"
) -> List[Dict[str, Any]]:
    """
    Runs the generation pipeline for recommendation_id (already created as IN_PROGRESS)
    and persists COMPLETED + filtered_courses on success, or FAILED + error_message on
    failure (re-raised so the caller can build the UnitOutcome).

    Accumulates per-stage LLM usage into the passed-in `tally` (mutated in place).
    Returns final_filtered_courses on success.
    """
    user_profile = _build_user_profile(role_mapping)
    try:
        vector_query_json, all_candidates, kw_emb, final_filtered_courses = await generate_recommendation_for_role_mapping(
            recommendation_id,
            user_profile,
            role_mapping.state_center_name or "",
            role_mapping.department_name or "",
            role_mapping.competencies or [],
            tally,
        )
    except Exception as gen_exc:
        logger.exception(f"FAIL  {label} -> Recommendation generation failed: {gen_exc}")
        if not DRY_RUN:
            async with get_session() as db:
                await db.execute(
                    update(RecommendedCourse)
                    .where(RecommendedCourse.id == recommendation_id)
                    .values(status=RecommendationStatus.FAILED, error_message=str(gen_exc)[:4000])
                )
                await db.commit()
        raise

    if not DRY_RUN:
        async with get_session() as db:
            await db.execute(
                update(RecommendedCourse)
                .where(RecommendedCourse.id == recommendation_id)
                .values(
                    vector_query=vector_query_json,
                    embedding=kw_emb,
                    actual_courses=all_candidates,
                    filtered_courses=final_filtered_courses,
                    status=RecommendationStatus.COMPLETED,
                )
            )
            await db.commit()
    logger.info(f"      {label} -> recommendation COMPLETED with {len(final_filtered_courses)} filtered course(s)")
    return final_filtered_courses


async def run_cbp_plan_creation(
    rm_id: uuid.UUID, recommendation_id: uuid.UUID, filtered_courses: List[Dict[str, Any]], label: str
) -> None:
    """Saves a CBP plan from ALL filtered_courses (mirrors save_cbp_plan). Marks the
    recommendation FAILED and re-raises on error. In DRY_RUN, nothing is written to the
    database."""
    try:
        selected_courses = [dict(c) for c in filtered_courses]
        if not DRY_RUN:
            async with get_session() as db:
                await with_retry(
                    create_cbp_plan,
                    db,
                    USER_ID,
                    rm_id,
                    recommendation_id,
                    selected_courses,
                    description="create_cbp_plan",
                )
    except Exception as plan_exc:
        logger.exception(f"FAIL  {label} -> CBP plan save failed: {plan_exc}")
        if not DRY_RUN:
            async with get_session() as db:
                await db.execute(
                    update(RecommendedCourse)
                    .where(RecommendedCourse.id == recommendation_id)
                    .values(status=RecommendationStatus.FAILED, error_message=str(plan_exc)[:4000])
                )
                await db.commit()
        raise


class ProgressTracker:
    """Assigns each unit of work a stable [index/total] label as it starts, so log lines
    show how many of the total units have been picked up so far (e.g. '[7/1023]').
    next_index() is a plain (non-async) increment: asyncio has no preemption between awaits,
    so this single statement can't race even though many tasks call it concurrently."""

    def __init__(self, total: int):
        self.total = total
        self._count = 0

    def next_index(self) -> int:
        self._count += 1
        return self._count


async def process_role_mapping(
    excel_row: ExcelRow, semaphore: asyncio.Semaphore, progress: "ProgressTracker"
) -> UnitOutcome:
    rm_id = excel_row.role_mapping_id
    async with semaphore:
        index = progress.next_index()
        label = f"[{index}/{progress.total}] role_mapping={rm_id} (excel row {excel_row.row_number})"
        logger.info(f"START {label}")

        try:
            # --- Idempotency check (id/status only — no full role_mapping fetch needed yet) ---
            # 1. No recommendation exists yet                              -> generate recommendation, then CBP plan
            # 2. Recommendation exists, status FAILED (or IN_PROGRESS,     -> delete stale recommendation (+ any CBP
            #    e.g. a crashed prior run)                                    plan) and retrigger generation from scratch
            # 3. Recommendation exists, status COMPLETED, no CBP plan yet  -> reuse recommendation, just generate CBP plan
            # 4. Recommendation COMPLETED AND CBP plan exists              -> skip entirely
            async with get_session() as db:
                existing_recommendation = await get_existing_recommendation(db, rm_id, USER_ID)

            recommendation_id: Optional[uuid.UUID] = None
            filtered_courses: Optional[List[Dict[str, Any]]] = None
            tally: Optional[TokenTally] = None

            if existing_recommendation and existing_recommendation.status == RecommendationStatus.COMPLETED:
                async with get_session() as db:
                    existing_plan = await get_existing_cbp_plan(db, rm_id, USER_ID)

                if existing_plan:
                    logger.info(f"SKIP  {label} -> recommendation COMPLETED and CBP plan already exist, skipping")
                    return UnitOutcome(
                        excel_row=excel_row,
                        result=UnitResult.SKIPPED_EXISTING,
                        recommendation_id=existing_recommendation.id,
                        total_courses=len(existing_recommendation.filtered_courses or []),
                        tokens=None,
                    )

                logger.info(f"      {label} -> recommendation already COMPLETED, no CBP plan yet -> generating CBP plan only")
                recommendation_id = existing_recommendation.id
                filtered_courses = existing_recommendation.filtered_courses or []

            else:
                if existing_recommendation:
                    logger.info(
                        f"      {label} -> existing recommendation status={existing_recommendation.status} "
                        f"(not COMPLETED) -> {'would delete and retrigger generation (dry-run: not deleting)' if DRY_RUN else 'deleting and retriggering generation'}"
                    )
                    if not DRY_RUN:
                        async with get_session() as db:
                            await delete_existing_records(db, rm_id, USER_ID)
                else:
                    logger.info(f"      {label} -> no existing recommendation found -> generating fresh")

                # --- Full role_mapping fetch — needed now (both to check the mandatory iGOT
                # designation fields, and, if generation proceeds, to build the user profile) ---
                async with get_session() as db:
                    role_mapping = await fetch_role_mapping_by_id(db, rm_id)
                if not role_mapping:
                    error_message = f"role_mapping {rm_id} not found"
                    logger.error(f"FAIL  {label} -> {error_message}")
                    return UnitOutcome(excel_row=excel_row, result=UnitResult.FAILED, error=error_message)
                label = f"[{index}/{progress.total}] role_mapping={rm_id} designation='{role_mapping.designation_name}' (excel row {excel_row.row_number})"

                # igot_designation_name/igot_designation_id are mandatory -- a role_mapping with
                # both blank is skipped entirely (no LLM call, no DB writes), in dry-run and execute.
                if not (role_mapping.igot_designation_name or role_mapping.igot_designation_id):
                    reason = "role_mapping has neither igot_designation_name nor igot_designation_id"
                    logger.info(f"SKIP  {label} -> {reason}, skipping")
                    return UnitOutcome(excel_row=excel_row, result=UnitResult.SKIPPED_NO_IGOT_DESIGNATION,
                                       error=reason)

                if DRY_RUN:
                    # Dry-run is a zero-cost plan preview: never call the LLM/embedding pipeline.
                    # Report that this role_mapping WOULD be freshly generated, with no
                    # recommendation_id/total_courses/tokens, and stop here.
                    logger.info(f"      {label} -> dry-run: would generate fresh recommendation (LLM not called)")
                    return UnitOutcome(excel_row=excel_row, result=UnitResult.WOULD_GENERATE)

                # --- Create fresh IN_PROGRESS recommendation row ---
                async with get_session() as db:
                    new_recommendation = RecommendedCourse(
                        id=uuid.uuid4(),
                        user_id=USER_ID,
                        role_mapping_id=rm_id,
                        status=RecommendationStatus.IN_PROGRESS,
                        vector_query="",
                        actual_courses=[],
                        filtered_courses=[],
                    )
                    db.add(new_recommendation)
                    await db.commit()
                    await db.refresh(new_recommendation)
                    recommendation_id = new_recommendation.id

                # --- Generate recommendation ---
                tally = TokenTally()
                try:
                    filtered_courses = await run_recommendation_generation(role_mapping, recommendation_id, label, tally)
                except Exception as gen_exc:
                    return UnitOutcome(
                        excel_row=excel_row,
                        result=UnitResult.FAILED,
                        recommendation_id=recommendation_id,
                        tokens=tally,
                        error=f"Recommendation generation failed: {gen_exc}",
                    )

            # --- Save CBP plan from ALL filtered_courses (mirrors save_cbp_plan) ---
            try:
                await run_cbp_plan_creation(rm_id, recommendation_id, filtered_courses, label)
            except Exception as plan_exc:
                return UnitOutcome(
                    excel_row=excel_row,
                    result=UnitResult.FAILED,
                    recommendation_id=recommendation_id,
                    total_courses=len(filtered_courses or []),
                    tokens=tally,
                    error=f"CBP plan save failed: {plan_exc}",
                )

            logger.info(f"DONE  {label} -> recommendation + CBP plan saved successfully")
            return UnitOutcome(
                excel_row=excel_row,
                result=UnitResult.SUCCEEDED,
                recommendation_id=recommendation_id,
                total_courses=len(filtered_courses or []),
                tokens=tally,
            )

        except Exception as unexpected_exc:
            error_message = f"Unexpected failure: {unexpected_exc}"
            logger.exception(f"FAIL  {label} -> {error_message}")
            if not DRY_RUN:
                try:
                    async with get_session() as db:
                        await db.execute(
                            update(RecommendedCourse)
                            .where(RecommendedCourse.role_mapping_id == rm_id, RecommendedCourse.user_id == USER_ID)
                            .values(status=RecommendationStatus.FAILED, error_message=str(unexpected_exc)[:4000])
                        )
                        await db.commit()
                except Exception:
                    logger.exception(f"      {label} -> also failed to mark recommendation as FAILED in DB")
            return UnitOutcome(excel_row=excel_row, result=UnitResult.FAILED, error=error_message)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

async def main():
    logger.info("=" * 100)
    logger.info("Batch: generate course recommendations + save CBP plans")
    logger.info(f"Excel file: {EXCEL_FILE}")
    logger.info(f"User ID: {USER_ID}")
    logger.info(f"Max concurrency: {MAX_CONCURRENCY}")
    logger.info(f"Log file: {LOG_FILE}")
    if DRY_RUN:
        logger.info("DRY RUN: zero-cost plan preview -- no database writes, and the LLM/embedding "
                     "pipeline is NOT called for role_mappings that would need fresh generation "
                     "(reported as WOULD_GENERATE in the outcome CSV). Already-completed "
                     "recommendations/CBP plans are reported as SKIPPED_EXISTING using existing data.")
    logger.info("=" * 100)

    excel_rows, skipped_input_rows = read_excel_rows(EXCEL_FILE)
    if not excel_rows:
        logger.warning("No data rows found in Excel. Nothing to do.")
        await engine.dispose()
        return

    # One Excel row == one role_mapping == one unit of work (the mandatory role_mapping_id
    # column directly identifies each role_mapping; there is no state/department scope lookup).
    units_count = len(excel_rows)
    logger.info(f"Total units of work (role_mappings) to process: {units_count}")

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    progress = ProgressTracker(units_count)
    pending_tasks = [
        asyncio.create_task(process_role_mapping(row, semaphore, progress))
        for row in excel_rows
    ]
    outcomes: List[UnitOutcome] = await asyncio.gather(*pending_tasks)

    succeeded = [o for o in outcomes if o.result == UnitResult.SUCCEEDED]
    skipped = [o for o in outcomes if o.result == UnitResult.SKIPPED_EXISTING]
    skipped_no_igot = [o for o in outcomes if o.result == UnitResult.SKIPPED_NO_IGOT_DESIGNATION]
    failed = [o for o in outcomes if o.result == UnitResult.FAILED]
    would_generate = [o for o in outcomes if o.result == UnitResult.WOULD_GENERATE]

    logger.info("=" * 100)
    logger.info("RUN SUMMARY")
    logger.info("=" * 100)
    logger.info(f"Excel rows read:               {len(excel_rows)}")
    logger.info(f"Skipped (invalid input row):   {len(skipped_input_rows)}")
    logger.info(f"Units of work (role maps):     {units_count}")
    logger.info(f"Succeeded:                     {len(succeeded)}")
    logger.info(f"Skipped (already done):        {len(skipped)}")
    logger.info(f"Skipped (no igot designation): {len(skipped_no_igot)}")
    logger.info(f"Failed:                        {len(failed)}")
    if DRY_RUN:
        logger.info(f"Would generate (dry-run, LLM not called): {len(would_generate)}")

    if skipped_input_rows:
        logger.info("-" * 100)
        logger.info("SKIPPED INVALID ROW DETAILS (also in the outcome CSV as SKIPPED_INVALID_ROW):")
        for s in skipped_input_rows:
            logger.info(f"  - row={s.row_number} role_mapping_id={s.raw_role_mapping_id!r} reason={s.reason}")

    if failed:
        logger.info("-" * 100)
        logger.info("FAILED DETAILS (also persisted to recommended_courses.error_message):")
        for o in failed:
            logger.info(
                f"  - row={o.excel_row.row_number} "
                f"role_mapping_id={o.excel_row.role_mapping_id} designation='{o.excel_row.designation}' "
                f"error={o.error}"
            )

    # Outcome summary written as CSV: one row per unit of work, echoing the 7 input columns
    # plus the recommendation_id, status, course counts, per-stage token usage, and error.
    # contextual_queries_tokens / filter_courses_tokens hold the COMPLETE Gemini usage_metadata
    # object as a JSON string (not just a total), per the requirement for full usage detail.
    # Rows dropped by read_excel_rows (missing/invalid role_mapping_id) get their own CSV row
    # too, with only row_number/role_mapping_id/status/error populated -- previously these were
    # silently absent from the CSV, only visible as a count in the log.
    with open(OUTCOME_CSV_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "state_center_id", "department_id", "org_type", "state_center_name",
            "department_name", "designation", "role_mapping_id", "recommendation_id",
            "status", "pgvector_courses_count", "total_courses", "contextual_queries_tokens",
            "filter_courses_tokens", "embedding_tokens", "total_tokens", "error",
        ])
        for o in outcomes:
            row = o.excel_row
            t = o.tokens
            writer.writerow([
                row.state_center_id,
                row.department_id,
                row.org_type,
                row.state_center_name,
                row.department_name,
                row.designation,
                str(row.role_mapping_id),
                str(o.recommendation_id) if o.recommendation_id else "",
                o.result.value,
                t.pgvector_courses_count if t else 0,
                o.total_courses,
                json.dumps(t.contextual_queries_usage, ensure_ascii=False) if t and t.contextual_queries_usage else "",
                json.dumps(t.filter_courses_usage, ensure_ascii=False) if t and t.filter_courses_usage else "",
                t.embedding_tokens if t else 0,
                t.total_tokens if t else 0,
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
                "",
                "SKIPPED_INVALID_ROW",
                0, 0, "", "", 0, 0,
                f"row {skipped.row_number}: {skipped.reason}",
            ])

    logger.info("=" * 100)
    logger.info(f"Full log written to: {LOG_FILE}")
    logger.info(f"Outcome CSV written to: {OUTCOME_CSV_FILE}")
    logger.info("=" * 100)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

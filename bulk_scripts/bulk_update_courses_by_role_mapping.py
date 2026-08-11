"""Bulk-remove and/or bulk-add courses on a role mapping's recommendations and CBP plans.

Each input row names one `role_mappings` row, the course identifiers to strip from it, and the
course identifiers to add to it. For every row the script updates:
    recommended_courses.filtered_courses   (the recommendation list shown in the UI)
    cbp_plans.selected_courses             (every CBP plan built on that role mapping)

Removals mirror the app's own single-course deletes (`DELETE /course-recommendations/
{role_mapping_id}/course/{course_id}` and `DELETE /cbp-plan/{cbp_plan_id}/course/
{course_identifier}`) -- matching is by the course's `identifier` field, so a `do_...` content id or
the UUID of a user-added course both work.

Additions are built from `course_metadata_weightage` -- the same table the recommendation pipeline
enriches its LLM-filtered courses from -- so an added course carries exactly the same keys as a
generated one (`identifier`, `course`, `relevancy`, `rationale`, `is_public`, `competencies`,
`duration`, `organisation`) and is indistinguishable downstream (reports, publishing, dashboards).
An identifier that is not in that table cannot be enriched and is therefore NOT added: it is
reported under `unresolved_courses`, never inserted as a bare id that would render as an empty card.
That table is a local mirror populated by the metadata ingestion job, so a course that is Live on
iGOT but missing here needs that ingestion re-run before it can be added.

Because a manually added course never went through the LLM, its `relevancy` is set to --relevancy
(default 90, the app's DEFAULT_RELEVANCY_SCORE, i.e. what the app itself uses for courses pulled in
by identifier) and its `rationale` to --rationale. In `filtered_courses` the list is re-sorted by
relevancy descending after an add, matching how generation persists it; in `selected_courses` added
courses are appended, matching how the app's plan-save builds that list.

`recommended_courses.actual_courses` is deliberately LEFT UNTOUCHED by both operations: it is the
raw vector-search audit trail of what the search returned, is never read back for display or
selection, and rewriting it would destroy that history. Only `filtered_courses` drives what the user
sees.

`suggested_courses` and `user_added_courses` are NOT touched either -- this script's scope is the two
tables above. An identifier that only exists as a suggestion or a user-added course is therefore
reported under `not_found_courses` on removal, not removed.

If a role mapping's recommendation row is still IN_PROGRESS, the whole row is skipped
(`recommendation_in_progress`) rather than half-edited under a running generation -- the same guard
the API returns 409 for. Re-generating recommendations afterwards discards every edit made here
(removed courses come back, added ones disappear); run this after generation has settled.

The update is PURE DB -- there is no API/login. It is a dry-run by default; pass --execute to
persist. Safe to re-run: an identifier already gone (or already present) is reported, not an error,
so a row with nothing left to do comes back as `no_change_needed`.

Rows are processed asynchronously in batches of at most --batch-size concurrent (default 10,
asyncio.Semaphore), each row in its own transaction so one failure rolls back alone. Under
--execute the rows being edited are locked (SELECT ... FOR UPDATE) for the duration of that
transaction, so a concurrent app edit cannot be silently clobbered by the read-modify-write.

Every run (dry-run and --execute) writes a log + an outcome CSV under bulk_scripts/logs/, named
after the input file (same convention as copy_role_mapping_by_designation.py).

Input file: .csv or .xlsx/.xlsm. Delimiter for a .csv/text file is auto-detected (tab for a
direct spreadsheet paste, comma for a saved CSV). Three columns are expected
(case/space/underscore-insensitive; several spellings accepted):
    Role Mapping ID         (required)  -> role_mappings.id (a UUID)
    Courses To Be Removed   (optional)  -> identifiers to remove
    Courses To Be Added     (optional)  -> identifiers to add

At least one of the two course columns must be non-empty on a row; either may be left blank to do
only the other. The same identifier may not appear in both columns of one row (`identifier_in_both`).

A courses cell may hold a single identifier, a separated list (comma, semicolon, pipe, newline or
space) or a JSON/Python-style list -- `["do_1", "do_2"]`, `do_1; do_2` and `do_1 do_2` all parse to
the same two identifiers. Duplicates within a cell are collapsed. A bracketed list left UNQUOTED in
a comma-delimited file (`[do_1,do_2]`, which csv would otherwise split across the next columns and
silently read as the *other* column's value) is stitched back together by bracket balance before the
columns are assigned -- so an Excel-mangled `[""do_1"",""do_2""]` cell still lands entirely in the
column it was written in. If a row still has more fields than the header after that repair, it is
rejected as `ambiguous_columns` rather than guessed at.

If the same role_mapping_id appears on several rows, each row is processed independently against the
identifiers on that row (they are not merged).

--user-id is REQUIRED. role_mappings.id is globally unique, but every run is scoped to a single
owner: a role mapping owned by anyone else is skipped as `user_mismatch` rather than touched.

Run (dry-run):
    python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel changes.csv --user-id <uuid>
Run (persist):
    python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel changes.csv --user-id <uuid> --execute
Run with a specific batch size (default 10):
    python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel changes.xlsx --user-id <uuid> --execute --batch-size 20
Run with a different relevancy score for added courses (default 90):
    python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel changes.csv --user-id <uuid> --execute --relevancy 80
"""
import argparse
import ast
import asyncio
import contextlib
import csv
import enum
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    bindparam,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# ---------------------------------------------------------------------------
# Standalone infrastructure
#
# This script is self-contained: it does NOT import from the application `src`
# package. The database config, async session manager, logger and the ORM
# models it needs are all defined inline below, so the file can be copied and
# run on its own with just SQLAlchemy + asyncpg installed.
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".." / ".env"


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


DATABASE_URL = require_env("DATABASE_URL")

logger = logging.getLogger("bulk_update_courses")

Base = declarative_base()


class DatabaseSessionManager:
    """Minimal async SQLAlchemy session manager (mirrors src.core.database)."""

    def __init__(self):
        self._engine = None
        self._sessionmaker = None

    def init(self, host):
        if self._engine is not None:
            return
        self._engine = create_async_engine(
            host,
            pool_size=20,
            max_overflow=40,
            pool_timeout=30,
            pool_recycle=1800,
            echo=False,
        )
        self._sessionmaker = async_sessionmaker(
            bind=self._engine,
            autoflush=False,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def close(self):
        if self._engine is None:
            return
        await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None

    @contextlib.asynccontextmanager
    async def session(self):
        if self._sessionmaker is None:
            raise Exception("DatabaseSessionManager is not initialized")
        session = self._sessionmaker()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


sessionmanager = DatabaseSessionManager()


# --- ORM models (only what this script touches) -----------------------------


class RecommendationStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RoleMapping(Base):
    """role_mappings -- read-only here, purely for report context (owner + scope +
    designation) and to tell 'role mapping does not exist' apart from 'it exists but has no
    recommendation/plan rows yet'. Relationships are intentionally omitted."""

    __tablename__ = "role_mappings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    state_center_id = Column(String(32), nullable=False, index=True)
    department_id = Column(String(32), nullable=True, index=True)
    state_center_name = Column(String(255), nullable=True, index=True)
    department_name = Column(String(255), nullable=True, index=True)
    status = Column(String(50), nullable=True, index=True)
    designation_name = Column(String(255), nullable=True, index=True)
    sort_order = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecommendedCourse(Base):
    """recommended_courses -- only `filtered_courses` is ever written. `actual_courses` and
    `embedding` are declared so nothing is accidentally nulled on flush, but are never modified."""

    __tablename__ = "recommended_courses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    role_mapping_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    status = Column(String, nullable=False)
    error_message = Column(Text, nullable=True)
    vector_query = Column(Text, nullable=True)
    embedding = Column(JSONB, nullable=True)
    actual_courses = Column(JSONB, nullable=True, default=list)
    filtered_courses = Column(JSONB, nullable=True, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CBPPlan(Base):
    """cbp_plans -- only `selected_courses` is ever written."""

    __tablename__ = "cbp_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    role_mapping_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    recommended_course_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    selected_courses = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# course_metadata_weightage is queried with raw SQL rather than mapped: it carries pgvector
# embedding columns this script has no business declaring (and cannot type without pgvector
# installed), and only these five fields are needed to build a course entry.
COURSE_METADATA_SQL = text(
    """
    SELECT identifier, name, competencies_v6, duration, organisation
    FROM public.course_metadata_weightage
    WHERE identifier IN :identifiers
    """
).bindparams(bindparam("identifiers", expanding=True))


# --------------------------------------------------------------------------
# Logging (matches the other bulk_scripts: one fresh timestamped file, named
# after the input file, under bulk_scripts/logs/; outcome CSV alongside it).
# --------------------------------------------------------------------------

LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Max rows processed concurrently (each in its own transaction).
DEFAULT_BATCH_SIZE = 10

# Relevancy stamped on a manually added course. 90 is the app's DEFAULT_RELEVANCY_SCORE -- what it
# uses for courses pulled in by identifier rather than scored by the LLM.
DEFAULT_RELEVANCY_SCORE = 90
DEFAULT_RATIONALE = "Added on request as part of a bulk course update."

# Maps the spreadsheet headers (normalized: lowercased, whitespace/underscores/dashes removed) to
# the internal field names used throughout the script. Headers not listed here are kept as-is.
HEADER_MAP = {
    "rolemappingid": "role_mapping_id",
    "rolemapping": "role_mapping_id",
    "rolemappingids": "role_mapping_id",
    # --- removals ---
    "coursestoberemoved": "courses_to_be_removed",
    "coursetoberemoved": "courses_to_be_removed",
    "coursestoremove": "courses_to_be_removed",
    "coursetoremove": "courses_to_be_removed",
    "coursestobedeleted": "courses_to_be_removed",
    "removecourses": "courses_to_be_removed",
    "removedcourses": "courses_to_be_removed",
    "courseidstoberemoved": "courses_to_be_removed",
    # --- additions ---
    "coursestobeadded": "courses_to_be_added",
    "coursetobeadded": "courses_to_be_added",
    "coursestoadd": "courses_to_be_added",
    "coursetoadd": "courses_to_be_added",
    "addcourses": "courses_to_be_added",
    "addedcourses": "courses_to_be_added",
    "newcourses": "courses_to_be_added",
    "courseidstobeadded": "courses_to_be_added",
}

# Identifier list separators: comma, semicolon, pipe, and any whitespace (a course identifier
# never contains a space, so a space-separated paste is unambiguous).
_SPLIT_RE = re.compile(r"[\s,;|]+")

# Column order for the per-run CSV results report.
REPORT_COLUMNS = [
    "row_no",
    "mode",
    "status",
    "reason",
    "role_mapping_id",
    "role_mapping_user_id",
    "state_center_id",
    "department_id",
    "designation_name",
    "requested_removals",
    "requested_additions",
    "recommendation_ids",
    "recommendation_statuses",
    "rec_courses_before",
    "rec_courses_after",
    "removed_from_recommendations",
    "added_to_recommendations",
    "cbp_plan_ids",
    "cbp_courses_before",
    "cbp_courses_after",
    "removed_from_cbp_plans",
    "added_to_cbp_plans",
    "added_course_names",
    "not_found_courses",
    "unresolved_courses",
    "already_present_courses",
]


def _clean(value):
    """Normalize a cell to a stripped string, or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _map_header(header):
    """Map a spreadsheet header to its internal field name (HEADER_MAP), tolerating case,
    whitespace, underscores and dashes ('Role Mapping ID', 'role_mapping_id' and 'role-mapping-id'
    all -> role_mapping_id). Unknown headers are kept stripped."""
    if header is None:
        return ""
    norm = re.sub(r"[\s_\-]+", "", str(header)).lower()
    return HEADER_MAP.get(norm, str(header).strip())


def _rejoin_bracketed(values, delimiter):
    """Stitch a bracketed list cell back together after csv split it across columns.

    A `[do_1,do_2]` cell written WITHOUT surrounding quotes is not one field to csv -- it becomes
    `[do_1` and `do_2]`, which would shift every later column left by one and silently read a
    removal as an addition. Fields are re-joined (with the delimiter that split them) while the
    square brackets are unbalanced, restoring the original cell. Fields with balanced brackets --
    the normal, properly quoted case -- pass through untouched.
    """
    out = []
    buffer = None
    depth = 0
    for value in values:
        cell = "" if value is None else str(value)
        delta = cell.count("[") - cell.count("]")
        if buffer is None:
            if delta > 0:
                buffer, depth = cell, delta
            else:
                out.append(value)
        else:
            buffer = f"{buffer}{delimiter}{cell}"
            depth += delta
            if depth <= 0:
                out.append(buffer)
                buffer, depth = None, 0
    if buffer is not None:
        # Unterminated '[' -- keep what was collected rather than dropping the cell entirely.
        out.append(buffer)
    return out


def _row_from_values(header, values):
    """Zip a value list onto the mapped header, parking anything past the last declared column
    under '__extra__' (never silently dropped -- see _extra_values)."""
    row = {header[i]: (values[i] if i < len(values) else None) for i in range(len(header))}
    if len(values) > len(header):
        row["__extra__"] = list(values[len(header):])
    return row


def read_rows(path):
    """Read the input file into a list of dict rows keyed by the internal field names.

    .xlsx/.xlsm are read from the active sheet via openpyxl (imported lazily); everything else
    is treated as delimited text with the delimiter auto-detected (tab for a spreadsheet paste,
    comma for a saved CSV). Headers are mapped via _map_header, and an unquoted bracketed list
    split across columns by csv is repaired first (_rejoin_bracketed).
    """
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
            rows.append(_row_from_values(header, list(values)))
        return rows

    # encoding="utf-8-sig" strips the BOM that Excel-exported CSVs prepend, which would
    # otherwise corrupt the first header. Auto-detect tab (spreadsheet paste) vs comma (saved CSV).
    with open(path, newline="", encoding="utf-8-sig") as f:
        first_line = f.readline()
        f.seek(0)
        delimiter = "\t" if "\t" in first_line else ","
        reader = csv.reader(f, delimiter=delimiter)
        try:
            header = [_map_header(h) for h in next(reader)]
        except StopIteration:
            return []
        rows = []
        for values in reader:
            if not values:
                continue
            values = _rejoin_bracketed(values, delimiter)
            if all(_clean(v) is None for v in values):  # skip fully-blank rows
                continue
            rows.append(_row_from_values(header, values))
        return rows


def _extra_values(row):
    """Cells captured past the last declared column (see _row_from_values).

    These appear when a comma-delimited file has an unquoted multi-course cell that bracket repair
    couldn't stitch back (e.g. `<uuid>,do_1,do_2` with no brackets at all). With only a removals
    column they are unambiguously more removals; once an additions column exists there is no way to
    tell which column they belonged to, so the row is rejected instead (see update_one).
    """
    extra = row.get("__extra__")
    if extra is None:
        return []
    if isinstance(extra, (list, tuple)):
        return [v for v in extra if _clean(v) is not None]
    return [extra] if _clean(extra) is not None else []


def parse_identifiers(raw, extras=()):
    """Parse a course cell into an ordered, de-duplicated list of course identifiers.

    Accepts a bare identifier, a JSON/Python list (`["do_1","do_2"]`), or a list separated by
    commas/semicolons/pipes/whitespace. Surrounding brackets and quotes are stripped either way,
    including the doubled quotes (`[""do_1"",""do_2""]`) Excel writes when it quotes a cell that
    already contained quotes. `extras` are additional cells that spilled past the header.
    """
    identifiers = []

    def add_all(values):
        for value in values:
            token = _clean(value)
            if token is None:
                continue
            token = token.strip("\"'")
            if token and token not in identifiers:
                identifiers.append(token)

    cell = _clean(raw)
    if cell is not None:
        parsed = None
        if cell.startswith("[") and cell.endswith("]"):
            # A JSON (or Python-repr) list pasted straight from a report/log.
            for loader in (json.loads, ast.literal_eval):
                try:
                    candidate = loader(cell)
                except Exception:
                    continue
                if isinstance(candidate, (list, tuple)):
                    parsed = candidate
                    break
        if parsed is not None:
            add_all(str(item) for item in parsed)
        else:
            add_all(_SPLIT_RE.split(cell.strip("[]")))

    add_all(extras)
    return identifiers


def _as_course_list(value):
    """Coerce a JSONB course column to a list of dicts.

    SQLAlchemy already hands back deserialized JSON, but a column written as a JSON *string* by
    an older path (or NULL) must not blow up a whole row -- anything unusable becomes [].
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    return value


def _identifier_of(course):
    """The identifier a course entry is matched on -- the same `identifier` field the app's
    delete endpoints use. Non-dict junk in the list has no identifier and is always kept."""
    if not isinstance(course, dict):
        return None
    return _clean(course.get("identifier"))


def _prune(courses, targets):
    """Split `courses` into (kept, removed_identifiers) against the `targets` identifier set."""
    kept = []
    removed = []
    for course in courses:
        identifier = _identifier_of(course)
        if identifier is not None and identifier in targets:
            removed.append(identifier)
        else:
            kept.append(course)
    return kept, removed


def _sort_by_relevancy(courses):
    """Sort a course list by relevancy descending, the order generation persists
    `filtered_courses` in. Stable, and tolerant of non-dict junk (sorted last)."""
    def key(course):
        if not isinstance(course, dict):
            return 0
        try:
            return float(course.get("relevancy") or 0)
        except (TypeError, ValueError):
            return 0

    return sorted(courses, key=key, reverse=True)


def _is_in_progress(status):
    """Tolerant IN_PROGRESS check: matches both the plain "IN_PROGRESS" string and an
    enum-repr like "RecommendationStatus.IN_PROGRESS"."""
    return str(status or "").split(".")[-1].strip().upper() == RecommendationStatus.IN_PROGRESS.value


def _join(values):
    """Render a list into one CSV cell (never a Python repr, so it stays pasteable)."""
    return ", ".join(str(v) for v in values)


async def fetch_course_metadata(db, identifiers):
    """Load {identifier: metadata row} from course_metadata_weightage for the given identifiers.

    Parameterized (expanding bindparam) rather than string-interpolated: these identifiers come
    straight from an operator's spreadsheet and are never trusted into SQL text.
    """
    if not identifiers:
        return {}
    rows = (await db.execute(COURSE_METADATA_SQL, {"identifiers": list(identifiers)})).all()
    return {row.identifier: row for row in rows}


def build_course_entry(identifier, meta, relevancy, rationale):
    """Build the course dict stored for an added course.

    Shaped exactly like a generated+enriched recommendation entry (same keys as the app's own
    `_enrich_topup_course`), so everything downstream -- reports, publishing, dashboards -- treats
    it identically to an LLM-selected course.
    """
    organisation = meta.organisation
    return {
        "identifier": identifier,
        "course": meta.name,
        "relevancy": relevancy,
        "rationale": rationale,
        "is_public": False,
        "competencies": meta.competencies_v6,
        "duration": meta.duration,
        "organisation": (
            ", ".join(str(o) for o in organisation if o)
            if isinstance(organisation, list)
            else (organisation or None)
        ),
    }


def _append_new(courses, entries):
    """Append the `entries` whose identifier isn't already in `courses`.

    Returns (new_list, added_identifiers, already_present_identifiers). Never duplicates and never
    overwrites an existing entry -- a course already on the list keeps its original relevancy and
    rationale rather than being reset to the script's defaults.
    """
    present = {i for i in (_identifier_of(c) for c in courses) if i is not None}
    result = list(courses)
    added = []
    already = []
    for entry in entries:
        if entry["identifier"] in present:
            already.append(entry["identifier"])
            continue
        result.append(entry)
        present.add(entry["identifier"])
        added.append(entry["identifier"])
    return result, added, already


async def update_one(db, row, execute, owner_user_id, relevancy, rationale):
    """Process one input row: remove the row's removal identifiers from, and add its addition
    identifiers to, every recommended_courses.filtered_courses and cbp_plans.selected_courses of
    that role mapping.

    `owner_user_id` is optional; when given, a role mapping owned by anyone else is skipped.
    Returns a uniform result dict (status + all report fields).
    """
    raw_rm_id = _clean(row.get("role_mapping_id"))
    extras = _extra_values(row)
    has_add_column = "courses_to_be_added" in row

    # With no additions column, spill-over cells are unambiguously more removals (the old
    # two-column behaviour). With one, they can't be attributed -- reject rather than guess.
    removals = parse_identifiers(
        row.get("courses_to_be_removed"), () if has_add_column else extras
    )
    additions = parse_identifiers(row.get("courses_to_be_added"))

    label = (
        f"role_mapping={raw_rm_id} "
        f"remove={_join(removals) or '-'} add={_join(additions) or '-'}"
    )

    # Uniform result skeleton so the report has a value for every column on every row.
    result = {
        "status": None,
        "reason": "",
        "role_mapping_id": raw_rm_id,
        "role_mapping_user_id": "",
        "state_center_id": "",
        "department_id": "",
        "designation_name": "",
        "requested_removals": _join(removals),
        "requested_additions": _join(additions),
        "recommendation_ids": "",
        "recommendation_statuses": "",
        "rec_courses_before": "",
        "rec_courses_after": "",
        "removed_from_recommendations": "",
        "added_to_recommendations": "",
        "cbp_plan_ids": "",
        "cbp_courses_before": "",
        "cbp_courses_after": "",
        "removed_from_cbp_plans": "",
        "added_to_cbp_plans": "",
        "added_course_names": "",
        "not_found_courses": "",
        "unresolved_courses": "",
        "already_present_courses": "",
    }

    def done(status, reason=""):
        result["status"] = status
        result["reason"] = reason
        return result

    if not raw_rm_id:
        logger.error(f"SKIP {label}: missing required column(s): role_mapping_id")
        return done("error", "missing_columns: role_mapping_id")

    if has_add_column and extras:
        logger.error(
            f"SKIP {label}: {len(extras)} unassignable field(s) past the last column "
            f"({_join(extras)}); quote or bracket the multi-course cells"
        )
        return done("error", f"ambiguous_columns: {_join(extras)}")

    if not removals and not additions:
        logger.error(f"SKIP {label}: no courses to remove and none to add")
        return done("error", "no_courses_specified: both course columns are empty")

    both = [i for i in removals if i in set(additions)]
    if both:
        logger.error(f"SKIP {label}: identifier(s) in both the remove and add column: {_join(both)}")
        return done("error", f"identifier_in_both: {_join(both)}")

    try:
        rm_id = uuid.UUID(raw_rm_id)
    except (ValueError, AttributeError, TypeError):
        logger.error(f"SKIP {label}: role_mapping_id is not a valid UUID")
        return done("error", f"invalid_role_mapping_id: {raw_rm_id}")

    targets = set(removals)

    role_mapping = (
        await db.execute(select(RoleMapping).where(RoleMapping.id == rm_id))
    ).scalar_one_or_none()
    if role_mapping is None:
        logger.warning(f"SKIP {label}: role mapping does not exist")
        return done("role_mapping_not_found", "no role_mappings row with this id")

    result["role_mapping_user_id"] = str(role_mapping.user_id)
    result["state_center_id"] = role_mapping.state_center_id
    result["department_id"] = role_mapping.department_id or ""
    result["designation_name"] = role_mapping.designation_name or ""
    label = f"'{role_mapping.designation_name}' ({label})"

    if owner_user_id is not None and role_mapping.user_id != owner_user_id:
        logger.warning(
            f"SKIP {label}: owned by {role_mapping.user_id}, not the requested --user-id {owner_user_id}"
        )
        return done("user_mismatch", f"role mapping is owned by {role_mapping.user_id}")

    # Load both sides. Under --execute the rows are locked for this transaction so the
    # read-modify-write of the JSONB lists cannot lose a concurrent app edit.
    rec_stmt = select(RecommendedCourse).where(RecommendedCourse.role_mapping_id == rm_id)
    plan_stmt = select(CBPPlan).where(CBPPlan.role_mapping_id == rm_id)
    if owner_user_id is not None:
        rec_stmt = rec_stmt.where(RecommendedCourse.user_id == owner_user_id)
        plan_stmt = plan_stmt.where(CBPPlan.user_id == owner_user_id)
    if execute:
        rec_stmt = rec_stmt.with_for_update()
        plan_stmt = plan_stmt.with_for_update()

    recommendations = (await db.execute(rec_stmt.order_by(RecommendedCourse.created_at))).scalars().all()
    plans = (await db.execute(plan_stmt.order_by(CBPPlan.created_at))).scalars().all()

    if not recommendations and not plans:
        logger.warning(f"SKIP {label}: role mapping has no recommendation and no CBP plan rows")
        return done("no_records", "no recommended_courses or cbp_plans rows for this role mapping")

    result["recommendation_ids"] = _join(r.id for r in recommendations)
    result["recommendation_statuses"] = _join(str(r.status) for r in recommendations)
    result["cbp_plan_ids"] = _join(p.id for p in plans)

    in_progress = [r for r in recommendations if _is_in_progress(r.status)]
    if in_progress:
        logger.warning(
            f"SKIP {label}: recommendation {_join(r.id for r in in_progress)} is IN_PROGRESS; "
            f"nothing changed (wait for generation to finish, then re-run)"
        )
        return done("recommendation_in_progress", "recommendation generation is still IN_PROGRESS")

    # --- resolve the courses to add ------------------------------------------
    # A course can only be added if its metadata exists locally; a bare identifier would render
    # as an empty card, so an unresolved one is reported and skipped, not inserted.
    metadata = await fetch_course_metadata(db, additions)
    unresolved = [i for i in additions if i not in metadata]
    entries = [
        build_course_entry(i, metadata[i], relevancy, rationale) for i in additions if i in metadata
    ]
    result["unresolved_courses"] = _join(unresolved)
    result["added_course_names"] = _join(f"{e['identifier']} ({e['course']})" for e in entries)
    if unresolved:
        logger.warning(
            f"{label}: {len(unresolved)} identifier(s) not in course_metadata_weightage, "
            f"cannot be added: {_join(unresolved)}"
        )

    # --- recommended_courses.filtered_courses --------------------------------
    removed_rec, added_rec, present_rec = [], [], []
    rec_before, rec_after = [], []
    for rec in recommendations:
        courses = _as_course_list(rec.filtered_courses)
        rec_before.append(len(courses))

        kept, removed = _prune(courses, targets)
        updated, added, already = _append_new(kept, entries)
        if added:
            # Generation persists filtered_courses sorted by relevancy desc; keep that invariant
            # so an added course lands in its ranked position rather than at the end.
            updated = _sort_by_relevancy(updated)

        rec_after.append(len(updated))
        removed_rec.extend(removed)
        added_rec.extend(added)
        present_rec.extend(already)
        if (removed or added) and execute:
            # Assigning a new list (never mutating in place) is what marks the JSONB column
            # dirty for SQLAlchemy; actual_courses is intentionally left as-is.
            rec.filtered_courses = updated

    # --- cbp_plans.selected_courses ------------------------------------------
    removed_plan, added_plan, present_plan = [], [], []
    plan_before, plan_after = [], []
    for plan in plans:
        courses = _as_course_list(plan.selected_courses)
        plan_before.append(len(courses))

        kept, removed = _prune(courses, targets)
        # Appended, not sorted: this mirrors how the app's plan-save builds selected_courses
        # (recommendation order, then courses pulled in by identifier, then user-added).
        updated, added, already = _append_new(kept, entries)

        plan_after.append(len(updated))
        removed_plan.extend(removed)
        added_plan.extend(added)
        present_plan.extend(already)
        if (removed or added) and execute:
            plan.selected_courses = updated

    result["rec_courses_before"] = _join(rec_before)
    result["rec_courses_after"] = _join(rec_after)
    result["removed_from_recommendations"] = _join(sorted(set(removed_rec)))
    result["added_to_recommendations"] = _join(sorted(set(added_rec)))
    result["cbp_courses_before"] = _join(plan_before)
    result["cbp_courses_after"] = _join(plan_after)
    result["removed_from_cbp_plans"] = _join(sorted(set(removed_plan)))
    result["added_to_cbp_plans"] = _join(sorted(set(added_plan)))
    result["already_present_courses"] = _join(sorted(set(present_rec) | set(present_plan)))

    not_found = [i for i in removals if i not in set(removed_rec) | set(removed_plan)]
    result["not_found_courses"] = _join(not_found)

    changed = bool(removed_rec or removed_plan or added_rec or added_plan)

    notes = []
    if not_found:
        notes.append(f"{len(not_found)} removal(s) not present: {_join(not_found)}")
    if unresolved:
        notes.append(f"{len(unresolved)} addition(s) unresolved: {_join(unresolved)}")
    if result["already_present_courses"]:
        notes.append(f"already present: {result['already_present_courses']}")
    reason = "; ".join(notes)

    if not changed:
        # Additions that resolved to nothing are a real failure the operator has to act on, so
        # they get their own status instead of hiding under 'nothing to do'.
        if unresolved and not entries and not removals:
            logger.warning(f"SKIP {label}: none of the requested additions could be resolved")
            return done("additions_unresolved", reason)
        logger.info(f"SKIP {label}: nothing to change ({reason or 'lists already as requested'})")
        return done("no_change_needed", reason or "lists already as requested")

    detail = (
        f"recommendations -{len(removed_rec)}/+{len(added_rec)} "
        f"(rows {result['rec_courses_before'] or '-'} -> {result['rec_courses_after'] or '-'}), "
        f"cbp_plans -{len(removed_plan)}/+{len(added_plan)} "
        f"(rows {result['cbp_courses_before'] or '-'} -> {result['cbp_courses_after'] or '-'})"
    )

    if not execute:
        logger.info(f"DRY-RUN would update {label}: {detail}")
        return done("would_update", reason)

    await db.commit()
    logger.info(f"UPDATED {label}: {detail}")
    return done("updated", reason)


def write_report_csv(results, outcome_csv_path):
    """Write a one-row-per-input-line .csv results report (every run) for verification.

    Plain CSV rather than .xlsx: openpyxl/Excel hard-caps a cell at 32,767 characters, which
    would silently truncate long identifier lists; CSV has no such limit, so the full content is
    always preserved verbatim.
    """
    with open(outcome_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            writer.writerow(
                ["" if r.get(col) is None else str(r.get(col, "")) for col in REPORT_COLUMNS]
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


def _count_cell(value):
    """How many identifiers a report cell holds (cells are ', '-joined by _join)."""
    return len([v for v in str(value or "").split(", ") if v])


async def main():
    parser = argparse.ArgumentParser(
        description="Bulk-remove and/or bulk-add courses on a role mapping's recommendations "
        "and CBP plans."
    )
    parser.add_argument("--excel", required=True, help="Path to the CSV/Excel change file")
    parser.add_argument(
        "--user-id",
        required=True,
        type=uuid.UUID,
        help="Owner UUID. Only role mappings owned by this user are touched; others are "
        "reported as user_mismatch.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist changes. Default is a dry-run (no writes).",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="How many rows to process concurrently (default: 10).")
    parser.add_argument("--relevancy", type=int, default=DEFAULT_RELEVANCY_SCORE,
                        help=f"Relevancy score stamped on added courses "
                             f"(default: {DEFAULT_RELEVANCY_SCORE}).")
    parser.add_argument("--rationale", default=DEFAULT_RATIONALE,
                        help="Rationale text stamped on added courses.")
    args = parser.parse_args()

    if not 0 <= args.relevancy <= 100:
        sys.exit(f"Aborting: --relevancy must be between 0 and 100, got {args.relevancy}")

    owner_uuid = args.user_id

    if not os.path.exists(args.excel):
        sys.exit(f"Input file not found: {args.excel}")

    input_stem = Path(args.excel).stem
    log_file = LOGS_DIR / f"{input_stem}_{RUN_TIMESTAMP}.log"
    outcome_csv_file = Path(args.excel).resolve().parent / f"{input_stem}_{RUN_TIMESTAMP}.csv"
    configure_logging(log_file)

    rows = read_rows(args.excel)
    if not rows:
        sys.exit("Input file has no data rows.")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    scope = f"user_id={owner_uuid}"
    logger.info(f"Loaded {len(rows)} row(s) from {args.excel}. Mode: {mode}. Scope: {scope}")
    logger.info(f"Added courses get relevancy={args.relevancy}, rationale={args.rationale!r}")

    sessionmanager.init(DATABASE_URL)
    results = [None] * len(rows)  # keep results in input order
    try:
        # Process rows concurrently, at most --batch-size in flight, each its own transaction.
        semaphore = asyncio.Semaphore(args.batch_size)

        async def process(index, row):
            async with semaphore:
                logger.info(f"--- Row {index}/{len(rows)} ---")
                async with sessionmanager.session() as db:
                    try:
                        res = await update_one(
                            db, row, args.execute, owner_uuid, args.relevancy, args.rationale
                        )
                    except Exception as e:
                        await db.rollback()
                        logger.exception(f"Row {index} failed")
                        res = {
                            "status": "error",
                            "reason": f"unexpected: {e}",
                            "role_mapping_id": _clean(row.get("role_mapping_id")),
                        }
                    else:
                        if not args.execute:
                            # Nothing was committed; drop the in-memory edits explicitly so a
                            # dry-run can never flush a pending change on session close.
                            await db.rollback()
                res["row_no"] = index
                res["mode"] = mode
                results[index - 1] = res

        await asyncio.gather(*(process(i, row) for i, row in enumerate(rows, start=1)))
    finally:
        await sessionmanager.close()

    results = [r for r in results if r is not None]

    summary = {}
    totals = {
        "removed_from_recommendations": 0,
        "added_to_recommendations": 0,
        "removed_from_cbp_plans": 0,
        "added_to_cbp_plans": 0,
    }
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
        for key in totals:
            totals[key] += _count_cell(r.get(key))
    logger.info(f"Summary: {summary}")
    verb = "Applied" if args.execute else "Would apply"
    logger.info(
        f"{verb}: recommended_courses.filtered_courses "
        f"-{totals['removed_from_recommendations']}/+{totals['added_to_recommendations']}, "
        f"cbp_plans.selected_courses "
        f"-{totals['removed_from_cbp_plans']}/+{totals['added_to_cbp_plans']}"
    )

    write_report_csv(results, outcome_csv_file)
    logger.info(f"Outcome CSV written to: {outcome_csv_file}")
    logger.info(f"Detailed logs written to: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())

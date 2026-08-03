"""Copy selected role-mapping designations from a source scope to a target scope.

Each `role_mappings` row is one designation's FRAC mapping within a scope of
(user_id, state_center_id, department_id). This script clones only the matching source
role_mappings rows themselves -- no child records. Suggested/user-added courses, course
recommendations and CBP plans are NOT copied here.

Source rows are matched by SCOPE ONLY (source_state_center_id, source_department_id,
designation_name) -- regardless of which user owns them, since the source's user_id is not an
input to this script. The target row is always created under --user-id. This mirrors
batch_copy_documents_and_summary.py's convention: one --user-id, source lookups unfiltered by
owner.

If the source scope + designation_name matches more than one COMPLETED, non-empty
role_mappings row (possibly owned by different users), only the MOST RECENTLY CREATED one is
used -- the rest are silently ignored, i.e. deduped by designation_name, keeping the newest.

The copy is PURE DB -- there is no API/login. It is a dry-run by default; pass --execute to
persist.

Rows are processed asynchronously in batches of at most --batch-size concurrent (default 10,
asyncio.Semaphore), each row in its own transaction so one failure rolls back alone.

A source is only copied if a real mapping exists: the row must be COMPLETED and carry
non-empty content (role_responsibilities / activities / competencies). A designation with
only a placeholder or empty row is skipped (source_not_completed / source_empty), as is a
missing one (source_not_found) or one the target scope already has (skipped_existing).

Every run (dry-run and --execute) writes a log + an outcome CSV under bulk_scripts/logs/,
named after the input file (same convention as batch_copy_documents_and_summary.py).

Input file: .csv or .xlsx/.xlsm. Delimiter for a .csv/text file is auto-detected (tab for a
direct spreadsheet paste, comma for a saved CSV). Expected "From ... / To ..." headers
(case/space-insensitive; snake_case internal names also accepted for backwards compatibility):
    From Center State ID      (required)          -> source state_center_id (org id)
    From Center state name    (optional)          -> context only
    From Department ID        (optional, blank=NULL)
    From Department Name      (optional)          -> context only
    From Designation ID       (optional)          -> informational (matching is by name)
    From Designation Name     (required)          -> source designation to copy
    Source Org Type           (required)          -> must be 'state' or 'ministry' (context only --
                                                       the new row's org_type always comes from
                                                       Target Org Type below, never cloned from source)
    To Center State ID        (required)          -> target state_center_id
    To Center state name      (required)          -> target state_center_name on the new row
    To Department ID          (optional, blank=NULL)
    ToDepartment Name         (optional)          -> target department_name on the new row
    To Designation ID         (optional)          -> informational
    To Designation Name       (required)          -> target designation
    Target Org Type           (required)          -> must be 'state' or 'ministry'; becomes the new
                                                       row's org_type

The user_id is NOT in the file; every copy is created under --user-id.
ID cells pasted in scientific notation (e.g. 1.36E+18) are rejected (id_scientific_notation) -- they
are a lossy display of the real id; format the ID columns as Text in the sheet before pasting.

Run (dry-run):
    python bulk_scripts/copy_role_mapping_by_designation.py --input mapping.csv --user-id <uuid>
Run (persist):
    python bulk_scripts/copy_role_mapping_by_designation.py --input mapping.csv --user-id <uuid> --execute
Run with a specific batch size (default 10):
    python bulk_scripts/copy_role_mapping_by_designation.py --input mapping.xlsx --user-id <uuid> --execute --batch-size 20
"""
import argparse
import asyncio
import contextlib
import csv
import enum
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
    and_,
    func,
    inspect,
    select,
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

logger = logging.getLogger("copy_role_mapping")

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


class ProcessingStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RoleMapping(Base):
    """role_mappings — full column set so clone_columns copies every field verbatim.
    Relationships are intentionally omitted: this script never traverses them."""

    __tablename__ = "role_mappings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    org_type = Column(String(20), nullable=True, index=True)
    state_center_id = Column(String(32), nullable=False, index=True)
    department_id = Column(String(32), nullable=True, index=True)
    state_center_name = Column(String(255), nullable=True, index=True)
    department_name = Column(String(255), nullable=True, index=True)
    status = Column(String(50), default=ProcessingStatus.COMPLETED, nullable=True, index=True)
    error_message = Column(Text, nullable=True)
    sector_name = Column(String(255), nullable=True, index=True)
    instruction = Column(Text, nullable=True)
    designation_name = Column(String(255), nullable=True, index=True)
    wing_division_section = Column(String(255), nullable=True)
    role_responsibilities = Column(JSONB, default=list, nullable=True)
    activities = Column(JSONB, default=list, nullable=True)
    competencies = Column(JSONB, default=list, nullable=True)
    sort_order = Column(Integer, nullable=True, index=True)
    igot_designation_name = Column(String(255), nullable=True)
    igot_designation_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------
# Logging (matches the other bulk_scripts: one fresh timestamped file, named
# after the input file, under bulk_scripts/logs/; outcome CSV alongside it).
# --------------------------------------------------------------------------

LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# Max rows processed concurrently (each in its own transaction).
DEFAULT_BATCH_SIZE = 10

# user_id comes from --user-id, not the file, so it is not a column.
REQUIRED_COLUMNS = [
    "source_state_center_id",
    "source_designation_name",
    "source_org_type",
    "target_state_center_id",
    "target_state_center_name",
    "target_designation_name",
    "target_org_type",
]

# org_type must parse to one of these (case/synonym-insensitive, see _org_type_of).
_ORG_TYPE_STATE_SYNONYMS = {"state", "states"}
_ORG_TYPE_MINISTRY_SYNONYMS = {"ministry", "ministries", "centre", "center", "central", "union"}

# Columns never copied verbatim: primary key, parent FK (set via relationship) and
# server-managed timestamps.
_SKIP_COLUMNS = {"id", "role_mapping_id", "created_at", "updated_at"}

# Maps the spreadsheet "From ... / To ..." headers (normalized: lowercased, whitespace removed)
# to the internal field names used throughout the script. Headers not listed here are kept as-is
# (so the internal snake_case names still work for backwards compatibility).
HEADER_MAP = {
    "fromcenterstateid": "source_state_center_id",
    "fromcenterstatename": "source_state_center_name",
    "fromdepartmentid": "source_department_id",
    "fromdepartmentname": "source_department_name",
    "fromdesignationid": "source_designation_id",
    "fromdesignationname": "source_designation_name",
    "sourceorgtype": "source_org_type",
    "tocenterstateid": "target_state_center_id",
    "tocenterstatename": "target_state_center_name",
    "todepartmentid": "target_department_id",
    "todepartmentname": "target_department_name",
    "todesignationid": "target_designation_id",
    "todesignationname": "target_designation_name",
    "targetorgtype": "target_org_type",
}

# Scope-ID fields that must never be a scientific-notation paste (e.g. "1.36E+18").
_ID_FIELDS = (
    "source_state_center_id",
    "source_department_id",
    "target_state_center_id",
    "target_department_id",
)
_SCI_NOTATION_RE = re.compile(r"^[-+]?\d*\.?\d+[eE][-+]?\d+$")

# Column order for the per-run CSV results report.
REPORT_COLUMNS = [
    "row_no",
    "status",
    "reason",
    "source_state_center_id",
    "source_state_center_name",
    "source_department_id",
    "source_department_name",
    "source_designation",
    "source_org_type",
    "source_id",
    "source_status",
    "target_user_id",
    "target_state_center_id",
    "target_state_center_name",
    "target_department_id",
    "target_department_name",
    "target_designation",
    "target_org_type",
    "new_role_mapping_id",
    "new_sort_order",
]


def _is_completed(status):
    """Tolerant COMPLETED check: matches both the plain "COMPLETED" string and an
    enum-repr like "ProcessingStatus.COMPLETED"."""
    return str(status or "").split(".")[-1].strip().upper() == "COMPLETED"


def _has_content(role_mapping):
    """True if the mapping carries real FRAC content (any of the three JSONB lists is
    non-empty). A COMPLETED-but-empty row is treated as 'designation only, no mapping'."""
    return bool(
        role_mapping.role_responsibilities
        or role_mapping.activities
        or role_mapping.competencies
    )


def _map_header(header):
    """Map a spreadsheet header to its internal field name (HEADER_MAP), tolerating case and
    whitespace ('ToDepartment Name' and 'To Department Name' both -> target_department_name).
    Unknown headers are kept stripped, so internal snake_case names still work."""
    if header is None:
        return ""
    norm = "".join(str(header).split()).lower()
    return HEADER_MAP.get(norm, str(header).strip())


def read_rows(path):
    """Read the mapping file into a list of dict rows keyed by the internal field names.

    .xlsx/.xlsm are read from the active sheet via openpyxl (imported lazily); everything else
    is treated as delimited text with the delimiter auto-detected (tab for a spreadsheet paste,
    comma for a saved CSV). Headers are mapped via _map_header.
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
            rows.append(
                {header[i]: (values[i] if i < len(values) else None) for i in range(len(header))}
            )
        return rows

    # encoding="utf-8-sig" strips the BOM that Excel-exported CSVs prepend, which would
    # otherwise corrupt the first header. Auto-detect tab (spreadsheet paste) vs comma (saved CSV).
    with open(path, newline="", encoding="utf-8-sig") as f:
        first_line = f.readline()
        f.seek(0)
        delimiter = "\t" if "\t" in first_line else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        rows = []
        for raw in reader:
            # Map headers to internal names; skip fully-blank rows.
            row = {_map_header(k): v for k, v in raw.items()}
            if all(_clean(v) is None for v in row.values()):
                continue
            rows.append(row)
        return rows


def _clean(value):
    """Normalize a cell to a stripped string, or None if empty."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _looks_scientific(value):
    """True if the value looks like a scientific-notation number (e.g. '1.36E+18') — a lossy
    Excel display of a large id that must never be used to match/store."""
    return bool(value) and bool(_SCI_NOTATION_RE.match(str(value).strip()))


def _org_type_of(value):
    """Parses an org_type cell to the canonical 'state'/'ministry' string (matches
    src.schemas.role_mapping.OrgType's values). Returns None if the value doesn't match a known
    synonym -- org_type is mandatory with no default, so an unparseable value must be surfaced
    as a row error, not silently guessed."""
    norm = "".join(str(value).split()).lower() if value is not None else ""
    if norm in _ORG_TYPE_STATE_SYNONYMS:
        return "state"
    if norm in _ORG_TYPE_MINISTRY_SYNONYMS:
        return "ministry"
    return None


def _scope_conditions(state_center_id, department_id, user_id=None):
    """Build the (state_center, department[, user]) scope filter, treating a blank
    department as an explicit NULL match. user_id is only applied when given -- source
    lookups are scope-only (no user filter), target lookups always pass user_id."""
    conditions = [RoleMapping.state_center_id == state_center_id]
    if department_id:
        conditions.append(RoleMapping.department_id == department_id)
    else:
        conditions.append(RoleMapping.department_id.is_(None))
    if user_id is not None:
        conditions.append(RoleMapping.user_id == user_id)
    return conditions


def clone_columns(instance, model_cls, overrides):
    """Return kwargs for a new `model_cls` copying every column from `instance` except
    PK/parent-FK/timestamps, then applying `overrides`."""
    data = {}
    for col in inspect(model_cls).columns:
        name = col.key
        if name in _SKIP_COLUMNS:
            continue
        data[name] = getattr(instance, name)
    data.update(overrides)
    return data


async def next_sort_order(db, user_id, state_center_id, department_id):
    """Atomically compute the next sort_order for the target scope, mirroring
    crud_role_mapping.create_with_next_sort_order (SELECT ... FOR UPDATE, then MAX+1)."""
    conditions = _scope_conditions(state_center_id, department_id, user_id=user_id)
    # Lock existing rows in the scope so concurrent runs queue here.
    await db.execute(select(RoleMapping.id).where(and_(*conditions)).with_for_update())
    result = await db.execute(
        select(func.coalesce(func.max(RoleMapping.sort_order), 0)).where(and_(*conditions))
    )
    return result.scalar() + 1


async def copy_one(db, row, execute, target_user_id):
    """Process one input row. `target_user_id` is the UUID every new row is created under.
    Source rows are matched by scope + designation_name only, regardless of owner. Returns a
    uniform result dict (status + all report fields)."""
    ssc = _clean(row.get("source_state_center_id"))
    ssc_name = _clean(row.get("source_state_center_name"))
    sdept = _clean(row.get("source_department_id"))
    sdept_name = _clean(row.get("source_department_name"))
    source_designation = _clean(row.get("source_designation_name"))
    source_org_type_raw = _clean(row.get("source_org_type"))

    tsc = _clean(row.get("target_state_center_id"))
    tdept = _clean(row.get("target_department_id"))
    tsc_name = _clean(row.get("target_state_center_name"))
    tdept_name = _clean(row.get("target_department_name"))
    target_designation = _clean(row.get("target_designation_name"))
    target_org_type_raw = _clean(row.get("target_org_type"))

    label = (
        f"'{source_designation}' [{ssc}/{sdept}] -> "
        f"'{target_designation}' [{target_user_id}/{tsc}/{tdept}]"
    )

    # Uniform result skeleton so the report has a value for every column on every row.
    result = {
        "status": None,
        "reason": "",
        "source_state_center_id": ssc,
        "source_state_center_name": ssc_name,
        "source_department_id": sdept,
        "source_department_name": sdept_name,
        "source_designation": source_designation,
        "source_org_type": source_org_type_raw,
        "source_id": "",
        "source_status": "",
        "target_user_id": str(target_user_id),
        "target_state_center_id": tsc,
        "target_state_center_name": tsc_name,
        "target_department_id": tdept,
        "target_department_name": tdept_name,
        "target_designation": target_designation,
        "target_org_type": target_org_type_raw,
        "new_role_mapping_id": "",
        "new_sort_order": "",
    }

    def done(status, reason=""):
        result["status"] = status
        result["reason"] = reason
        return result

    missing = [c for c in REQUIRED_COLUMNS if not _clean(row.get(c))]
    if missing:
        logger.error(f"SKIP {label}: missing required column(s): {', '.join(missing)}")
        return done("error", f"missing_columns: {', '.join(missing)}")

    # Both org_type columns are mandatory and must parse to 'state' or 'ministry' -- an
    # unparseable value is a row error (not a fallback), same convention as
    # batch_rolemapping_generate.py's _org_type_of.
    source_org_type = _org_type_of(source_org_type_raw)
    target_org_type = _org_type_of(target_org_type_raw)
    bad_org_type = []
    if source_org_type is None:
        bad_org_type.append(f"source_org_type={source_org_type_raw!r}")
    if target_org_type is None:
        bad_org_type.append(f"target_org_type={target_org_type_raw!r}")
    if bad_org_type:
        detail = ", ".join(bad_org_type)
        logger.error(f"SKIP {label}: invalid org_type ({detail}); must be 'state' or 'ministry'")
        return done("error", f"invalid_org_type: {detail}")
    # Report the canonical parsed value (not the raw cell) now that both are validated.
    result["source_org_type"] = source_org_type
    result["target_org_type"] = target_org_type

    # Guard: an ID pasted in scientific notation (e.g. 1.36E+18) is a lossy value that can never match.
    sci = [f for f in _ID_FIELDS if _looks_scientific(_clean(row.get(f)))]
    if sci:
        detail = ", ".join(f"{f}={_clean(row.get(f))}" for f in sci)
        logger.error(f"SKIP {label}: ID in scientific notation ({detail}); format the column as Text")
        return done("error", f"id_scientific_notation: {detail}")

    # Load ALL source candidates for (source scope + designation), regardless of owner, then
    # apply the source-validity ladder.
    src_conditions = _scope_conditions(ssc, sdept) + [
        RoleMapping.designation_name == source_designation
    ]
    stmt = (
        select(RoleMapping)
        .where(and_(*src_conditions))
        .order_by(RoleMapping.created_at.desc())
    )
    candidates = (await db.execute(stmt)).scalars().all()

    if not candidates:
        logger.warning(f"SKIP {label}: no source role mapping row found (designation absent)")
        return done("source_not_found", "no source row for scope + designation")

    completed = [c for c in candidates if _is_completed(c.status)]
    if not completed:
        statuses = sorted({str(c.status) for c in candidates})
        logger.warning(f"SKIP {label}: source exists but none COMPLETED (statuses={statuses})")
        return done("source_not_completed", f"source not COMPLETED (statuses={statuses})")

    with_content = [c for c in completed if _has_content(c)]
    if not with_content:
        logger.warning(f"SKIP {label}: source COMPLETED but has no FRAC content (empty mapping)")
        return done("source_empty", "source COMPLETED but has empty content")

    if len(with_content) > 1:
        logger.warning(
            f"{label}: {len(with_content)} valid source rows match (possibly different owners); "
            f"using the most recently created one (dedup by designation_name)"
        )
    # candidates is already ordered created_at DESC, so the first COMPLETED-with-content
    # entry (preserving that order through the two filters above) is the most recent one.
    src = with_content[0]
    result["source_id"] = str(src.id)
    result["source_status"] = str(src.status)

    # Skip if the target scope already has this designation (also covers same-scope self-copy).
    tgt_existing = _scope_conditions(tsc, tdept, user_id=target_user_id) + [
        RoleMapping.designation_name == target_designation
    ]
    if (await db.execute(select(RoleMapping.id).where(and_(*tgt_existing)))).first():
        logger.info(f"SKIP {label}: target already has designation '{target_designation}'")
        return done("skipped_existing", "target already has this designation")

    # Build the new parent row. org_type always comes from the input's target_org_type -- never
    # cloned from the source -- since the target scope's org_type may legitimately differ from
    # the source's (e.g. copying a state-level designation into a ministry).
    new_rm = RoleMapping(
        **clone_columns(
            src,
            RoleMapping,
            {
                "user_id": target_user_id,
                "org_type": target_org_type,
                "state_center_id": tsc,
                "state_center_name": tsc_name,
                "department_id": tdept,
                "department_name": tdept_name,
                "designation_name": target_designation,
                "status": ProcessingStatus.COMPLETED.value,
            },
        )
    )

    if not execute:
        logger.info(f"DRY-RUN would copy {label} (source id={src.id})")
        return done("would_copy")

    new_rm.sort_order = await next_sort_order(db, target_user_id, tsc, tdept)
    db.add(new_rm)
    await db.commit()
    await db.refresh(new_rm)
    result["new_role_mapping_id"] = str(new_rm.id)
    result["new_sort_order"] = new_rm.sort_order
    logger.info(f"COPIED {label}: new id={new_rm.id}, sort_order={new_rm.sort_order}")
    return done("copied")


def write_report_csv(results, outcome_csv_path):
    """Write a one-row-per-input-line .csv results report (every run) for verification.

    Plain CSV rather than .xlsx: openpyxl/Excel hard-caps a cell at 32,767 characters, which
    would silently truncate the JSONB course-content columns; CSV has no such limit, so the
    full DB content is always preserved verbatim.
    """
    with open(outcome_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(REPORT_COLUMNS)
        for r in results:
            writer.writerow(["" if r.get(col) is None else str(r.get(col, "")) for col in REPORT_COLUMNS])


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


async def main():
    parser = argparse.ArgumentParser(
        description="Copy role-mapping designations between scopes."
    )
    parser.add_argument("--input", required=True, help="Path to the CSV/Excel mapping file")
    parser.add_argument("--user-id", required=True, help="Target user's UUID (owner of every copied designation).")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist changes. Default is a dry-run (no writes).",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help="How many rows to process concurrently (default: 10).")
    args = parser.parse_args()

    try:
        target_uuid = uuid.UUID(args.user_id)
    except ValueError:
        sys.exit(f"Aborting: --user-id is not a valid UUID: {args.user_id}")

    if not os.path.exists(args.input):
        sys.exit(f"Input file not found: {args.input}")

    input_stem = Path(args.input).stem
    log_file = LOGS_DIR / f"{input_stem}_{RUN_TIMESTAMP}.log"
    outcome_csv_file = Path(args.input).resolve().parent / f"{input_stem}_{RUN_TIMESTAMP}.csv"
    configure_logging(log_file)

    rows = read_rows(args.input)
    if not rows:
        sys.exit("Input file has no data rows.")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    logger.info(f"Loaded {len(rows)} row(s) from {args.input}. Mode: {mode}. Target user_id={target_uuid}")

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
                        res = await copy_one(db, row, args.execute, target_uuid)
                    except Exception as e:
                        await db.rollback()
                        logger.exception(f"Row {index} failed")
                        res = {
                            "status": "error",
                            "reason": f"unexpected: {e}",
                            "source_designation": _clean(row.get("source_designation_name")),
                        }
                res["row_no"] = index
                res["mode"] = mode
                results[index - 1] = res

        await asyncio.gather(*(process(i, row) for i, row in enumerate(rows, start=1)))
    finally:
        await sessionmanager.close()

    results = [r for r in results if r is not None]

    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    logger.info(f"Summary: {summary}")

    write_report_csv(results, outcome_csv_file)
    logger.info(f"Outcome CSV written to: {outcome_csv_file}")
    logger.info(f"Detailed logs written to: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())

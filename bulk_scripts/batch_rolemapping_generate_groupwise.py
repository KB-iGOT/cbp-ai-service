"""
Bulk GROUP-WISE role-mapping runner (backend workaround, run in DEV).

Companion to bulk_scripts/batch_rolemapping_generate.py -- SAME input file, SAME harness
(dedup, idempotent resume, append logging, token capture, per-row status write-back, dry-run)
and the SAME two passes. The one difference is how designations are fed to the model:

    batch_rolemapping_generate.py  ->  1 LLM call per DESIGNATION  (batch of 1)
    this script                    ->  1 LLM call per GROUP-BATCH  (batch of --group-size)

The designations still come FROM THE FILE -- PASS 1 (designation extraction) is skipped here
exactly as it is there. Rows are grouped by (state_center_id, department_id, org_type), each
group is chunked into batches of --group-size (default 30), and each chunk becomes ONE call to
_generate_frac_for_batch(). That is the call the v3 service already batches with in production,
so this is the same code path, just driven from the file instead of from PASS 1 output.

    6,206 rows  ->  184 groups  ->  307 batches  ->  307 LLM calls

Why this matters: the ~157K-token prompt (KCM competency file + rules + document summaries) is
sent ONCE PER CALL. Batching 30 designations into one call amortises that prompt across 30
designations instead of paying it 30 times.

Batches do NOT span departments -- the prompt carries one department's document summaries and
context, so mixing scopes in a call would feed the model the wrong material. A department with
25 designations therefore still costs one full call; that partial-batch overhead is expected.

    PASS 1 (designation extraction)  -> SKIPPED (designations are given in the file)
    PASS 2 (FRAC generation)         -> RUN     (1 LLM call per batch of --group-size)
    PASS 3 (domain-from-WAO)         -> SKIPPED (not required)
    PASS 4 (KCM reconciliation)      -> RUN     (deterministic, no LLM)

then it saves one `role_mappings` row per designation (status=COMPLETED), exactly like the v3
flow -- WITHOUT the API's extra bookkeeping (placeholder rows, multi-row split).

iGOT designation matching runs by default, mirroring the v3 API, which auto-matches after every
generation: the saved designation names are deduped, matched against the iGOT master in one
call per batch, and written back with one bulk update. Unmatched names are a normal outcome and
are left blank. Pass --no-igot-match to skip it (avoids the KB search / embedding calls).

A batch is atomic for generation but NOT for saving: if the model returns 28 mappings for a
batch of 30, the 28 are saved and the 2 missing designations are marked FAILED individually, so
a partial response never loses the whole batch.

Prerequisites:
  * Document summaries must already exist for each scope (run bulk_summary_runner.py first) --
    PASS 2 reads them. A scope with no COMPLETED summaries is reported `unresolved`.
  * .env points at the dev DB + GCS bucket + Vertex creds (all required -- the app's Settings
    object fails fast at import if any are missing).

Mandatory CLI args: --excel and --user-id.

Source file columns (ALL mandatory -- missing any of them is a fatal error):
    state_center_id, department_id, org_type, state_center_name, department_name, designation
org_type must parse to 'state' or 'ministry'; an unparseable value marks that row `unresolved`.
NOTE: the mp_rolemappings_prod.csv export has a BLANK org_type on most rows -- pass
--default-org-type state to supply it rather than editing the file.

USAGE:
  # dry run (no LLM): resolve scopes, check summaries, show the batch plan
  .venv/bin/python bulk_scripts/batch_rolemapping_generate_groupwise.py \
      --excel /Users/vaibhavbhuva/Downloads/mp_rolemappings_prod.csv \
      --user-id 2874bac1-e1e0-481f-bea9-58e29725990c --default-org-type state

  # smoke test 2 batches, then the full run
  ... --default-org-type state --execute --limit-batches 2
  ... --default-org-type state --execute
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import csv
import logging
import os
import pkgutil
import random
import re
import sys
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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

from sqlalchemy import select, and_, delete, func  # noqa: E402
from src.core.database import sessionmanager  # noqa: E402
from src.models.document import Document  # noqa: E402
from src.models.role_mapping import RoleMapping, ProcessingStatus  # noqa: E402
from src.schemas.role_mapping import OrgType  # noqa: E402
from src.crud.role_mapping import crud_role_mapping  # noqa: E402

# Import the app logger up front: it runs logging.config.fileConfig() which disables existing
# loggers + replaces handlers. Triggering it now (cached) means our setup_logging() sticks.
import src.core.logger  # noqa: E402,F401

log = logging.getLogger("rolemap_group_runner")

_usage_sink: contextvars.ContextVar = contextvars.ContextVar("rm_usage", default=None)
RUN_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
DEFAULT_LOG_FILE = str(Path(__file__).resolve().parent / "logs" /
                       f"role_mapping_groupwise_{RUN_TIMESTAMP}.log")


def setup_logging(path: str) -> str:
    """Console + file (one fresh file per run). Installed after the app's fileConfig."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    con = logging.StreamHandler()
    con.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root.addHandler(con)
    root.addHandler(fh)
    root.setLevel(logging.INFO)
    for name in ("rolemap_group_runner", "ai_cbp_service"):
        lg = logging.getLogger(name)
        lg.disabled = False
        lg.handlers = []
        lg.propagate = True
        lg.setLevel(logging.INFO)
    logging.getLogger("google").setLevel(logging.WARNING)
    return path


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG (all overridable by CLI flags)
# ══════════════════════════════════════════════════════════════════════════════
EXCEL_PATH = ""
EXCEL_SHEET = ""
OUT_PATH = ""
GROUP_SIZE = 10          # designations per LLM call -- the whole point of this script
CONCURRENT_BATCHES = 10   # how many batch-calls run at once (each is ~157K input tokens)
PER_BATCH_TIMEOUT = 1800  # a 30-designation call generates far more than a single one
RETRIES = 2              # a lost batch costs a full ~157K-token prompt to redo -- worth retrying
# Backoff between attempts (seconds). Connection resets start higher: they mean the pipe was
# saturated, so retrying quickly just reproduces the collision.
RETRY_BACKOFF_BASE = 2.0
RETRY_BACKOFF_CONNECTION = 10.0
RETRY_BACKOFF_MAX = 120.0
# Stagger between batch starts. Without it, N batches acquire the semaphore on the same tick
# and put N ~157K-token request bodies on the wire simultaneously -- which is what triggers the
# "Connection reset by peer" storms.
BATCH_START_STAGGER = 2.0

_CAND = {
    "state":       {"statecenterid", "statecenter", "stateid", "state", "orgid", "frameworkid"},
    "dept":        {"departmentmdoid", "mdoid", "deptmdoid", "departmentid", "deptid",
                    "department", "dept"},
    "statename":   {"statecentername", "statename"},
    "deptname":    {"departmentmdoname", "mdoname", "departmentname", "deptname"},
    "designation": {"designationname", "designation", "role", "post", "jobtitle"},
    "orgtype":     {"orgtype", "organizationtype", "organisationtype", "type", "level", "category"},
    "wing":        {"wingdivisionsection", "wing", "division", "section"},
}


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower()) if s is not None else ""


def _s(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _org_type_of(val) -> Optional[OrgType]:
    """Parses a row's org_type value. Returns None (not a fallback) if it doesn't match a known
    state/ministry synonym -- an unparseable value is surfaced as an error, not silently
    defaulted. --default-org-type supplies a value only where the column is EMPTY."""
    n = _norm(val)
    if n in ("state", "states"):
        return OrgType.state
    if n in ("ministry", "ministries", "centre", "center", "central", "union"):
        return OrgType.ministry
    return None


# ── source loading (.xlsx multi-tab / .csv) ──────────────────────────────────
_SHEET_KEY = "__sheet__"
_ROW_KEY = "__row__"


def load_csv_rows(path: str) -> tuple[list[str], list[dict]]:
    csv.field_size_limit(sys.maxsize)   # the competencies column holds large JSON blobs
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return (reader.fieldnames or []), list(reader)


def load_rows(path: str, sheet: str = "") -> tuple[list[str], list[dict]]:
    """(headers, rows) from .csv or .xlsx (all tabs unless `sheet`). Rows carry origin keys."""
    if path.lower().endswith(".csv"):
        hdrs, rows = load_csv_rows(path)
        for i, r in enumerate(rows, start=2):
            r[_SHEET_KEY], r[_ROW_KEY] = "(csv)", i
        return hdrs, rows
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    sheets = [wb[sheet]] if sheet else list(wb.worksheets)
    all_headers, seen, rows, per_sheet = [], set(), [], []
    for ws in sheets:
        it = ws.iter_rows(values_only=True)
        try:
            first = next(it)
        except StopIteration:
            continue
        headers = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(first)]
        for h in headers:
            if h not in seen:
                seen.add(h)
                all_headers.append(h)
        cnt = 0
        for rnum, raw in enumerate(it, start=2):
            if raw is None or all(c is None for c in raw):
                continue
            row = {headers[i]: raw[i] if i < len(raw) else None for i in range(len(headers))}
            row[_SHEET_KEY], row[_ROW_KEY] = ws.title, rnum
            rows.append(row)
            cnt += 1
        per_sheet.append((ws.title, cnt))
    wb.close()
    if len(per_sheet) > 1:
        log.info(f"read {len(per_sheet)} tabs: " + ", ".join(f"{t}={c}" for t, c in per_sheet))
    return all_headers, rows


def _origin(r: dict, fallback_idx: int) -> tuple[str, int]:
    return r.get(_SHEET_KEY, ""), r.get(_ROW_KEY, fallback_idx)


def detect_col(headers: list[str], override: str, kind: str) -> Optional[str]:
    if override:
        m = next((h for h in headers if h == override or _norm(h) == _norm(override)), None)
        if not m:
            raise SystemExit(f"[config] column '{override}' (for {kind}) not found. Headers: {headers}")
        return m
    cands = _CAND.get(kind, set())
    return next((h for h in headers if _norm(h) in cands), None)


# ── DB helpers (scope summaries, existing mappings) ──────────────────────────
async def _scope_summary_count(user_id: uuid.UUID, state_id: str, dept_id: Optional[str]) -> int:
    """How many COMPLETED document summaries exist in this scope for this user."""
    conds = [Document.state_center_id == state_id, Document.summary_status == "COMPLETED",
             Document.uploader_id == user_id]
    conds.append(Document.department_id == dept_id if dept_id else Document.department_id.is_(None))
    async with sessionmanager.session() as db:
        return int((await db.execute(select(func.count(Document.file_id)).where(and_(*conds)))).scalar() or 0)


async def _scope_summary_text(user_id: uuid.UUID, state_id: str, dept_id: Optional[str]) -> str:
    """Full document summaries for a scope, formatted like the v3 service's get_documents_summary."""
    conds = [Document.state_center_id == state_id, Document.summary_status == "COMPLETED",
             Document.uploader_id == user_id]
    conds.append(Document.department_id == dept_id if dept_id else Document.department_id.is_(None))
    async with sessionmanager.session() as db:
        docs = (await db.execute(select(Document).where(and_(*conds)))).scalars().all()
    parts = []
    for idx, doc in enumerate(docs, start=1):
        summary = (doc.summary_text or "").strip()
        parts.append(f"<document_summary_{idx}>\n Document Type: {doc.document_type} \n "
                     f"Summary: {summary}\n</document_summary_{idx}>")
    return "\n\n".join(parts)


async def _existing_completed(user_id: uuid.UUID, state_id: str, dept_id: Optional[str]) -> set:
    """designation_name (lowercased) that already have a COMPLETED role mapping for this scope."""
    conds = [RoleMapping.user_id == user_id, RoleMapping.state_center_id == state_id,
             RoleMapping.status == ProcessingStatus.COMPLETED]
    conds.append(RoleMapping.department_id == dept_id if dept_id else RoleMapping.department_id.is_(None))
    async with sessionmanager.session() as db:
        rows = (await db.execute(select(RoleMapping.designation_name).where(and_(*conds)))).all()
    return {(_s(r[0]) or "").lower() for r in rows}


async def _delete_existing(user_id: uuid.UUID, state_id: str, dept_id: Optional[str],
                           designations: list[str]):
    """Clear prior mappings for the given designations in one statement (--force)."""
    names = [(d or "").lower() for d in designations if d]
    if not names:
        return
    conds = [RoleMapping.user_id == user_id, RoleMapping.state_center_id == state_id,
             func.lower(RoleMapping.designation_name).in_(names)]
    conds.append(RoleMapping.department_id == dept_id if dept_id else RoleMapping.department_id.is_(None))
    async with sessionmanager.session() as db:
        await db.execute(delete(RoleMapping).where(and_(*conds)))
        await db.commit()


# scope summary cache (fetched once per scope, reused across its batches)
_summary_cache: dict[tuple, str] = {}
_summary_lock = asyncio.Lock()


async def _get_scope_summary(user_id: uuid.UUID, state_id: str, dept_id: Optional[str]) -> str:
    key = (state_id, dept_id)
    if key in _summary_cache:
        return _summary_cache[key]
    async with _summary_lock:
        if key not in _summary_cache:
            _summary_cache[key] = await _scope_summary_text(user_id, state_id, dept_id)
        return _summary_cache[key]


# ── target + batch models ────────────────────────────────────────────────────
@dataclass
class Target:
    """One designation -- the unit of RESULT reporting (a row in the report)."""
    excel_row: int
    sheet: str = ""
    state_id: Optional[str] = None
    dept_id: Optional[str] = None
    designation: Optional[str] = None
    wing: Optional[str] = None
    org_type: OrgType = OrgType.state
    resolution: str = "pending"         # matched | unresolved | duplicate
    prior_status: Optional[str] = None  # COMPLETED | NOT_STARTED
    final_status: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    comp_count: int = 0
    role_mapping_id: Optional[str] = None
    stored_name: Optional[str] = None   # designation_name as SAVED (the model may rename it)
    igot_designation_name: Optional[str] = None
    igot_designation_id: Optional[str] = None
    igot_match_type: Optional[str] = None   # exact | semantic (from the matcher)
    batch_no: int = 0                   # which group-batch carried this designation
    batch_size: int = 0
    tok_input: int = 0
    tok_output: int = 0
    tok_thinking: int = 0
    tok_total: int = 0
    names: tuple = field(default=("", ""))


@dataclass
class Batch:
    """One LLM call: up to --group-size designations from ONE (state, dept, org_type) scope."""
    no: int
    state_id: str
    dept_id: Optional[str]
    org_type: OrgType
    names: tuple
    targets: list = field(default_factory=list)
    group_index: int = 0     # this batch's position within its group (1-based)
    group_total: int = 0     # how many batches the group has


async def resolve_targets(rows: list[dict], cols: dict, user_id: uuid.UUID,
                          default_org_type: Optional[OrgType]) -> list[Target]:
    """One Target per unique (state, dept, designation). Identical resolution rules to the
    designation-wise runner, so both scripts agree on what is skippable."""
    seen: set = set()
    targets: list[Target] = []
    for idx, r in enumerate(rows, start=2):
        sid = _s(r.get(cols["state"])) if cols.get("state") else None
        did = _s(r.get(cols["dept"])) if cols.get("dept") else None
        desig = _s(r.get(cols["designation"])) if cols.get("designation") else None
        # cols["wing"] is the CSV's wing_division_section column (see _CAND["wing"]).
        wing = _s(r.get(cols["wing"])) if cols.get("wing") else None
        # Wing is part of a designation's IDENTITY, not just context. One department routinely
        # holds the same title in many postings -- 31 "VETERINARY OFFICER" rows under Assam
        # Animal Husbandry differing only by wing_division_section (IVB Khanapara, Marketing,
        # Mobile, Reserve, ...) -- and each is a distinct role with its own responsibilities.
        # Keying on designation alone collapsed them into one and silently dropped the rest
        # (192 of 543 Assam rows, 35% of the file). wing_division_section is already fed to the
        # model per designation, so it belongs in the key. Rows matching on BOTH designation
        # and wing are still treated as genuine repeats and skipped.
        key = (sid, did, (desig or "").lower(), (wing or "").lower())
        sheet, rownum = _origin(r, idx)
        if desig is None:
            targets.append(Target(excel_row=rownum, sheet=sheet, state_id=sid, dept_id=did,
                                  resolution="unresolved", error="missing designation"))
            continue
        if key in seen:
            targets.append(Target(excel_row=rownum, sheet=sheet, state_id=sid, dept_id=did,
                                  designation=desig, wing=wing, resolution="duplicate"))
            continue
        seen.add(key)
        sn = _s(r.get(cols["statename"])) if cols.get("statename") else None
        dn = _s(r.get(cols["deptname"])) if cols.get("deptname") else None
        raw_ot = r.get(cols["orgtype"]) if cols.get("orgtype") else None
        ot = _org_type_of(raw_ot)
        # --default-org-type fills an EMPTY cell only; a present-but-unparseable value is still
        # an error, so a typo is never silently coerced.
        if ot is None and _s(raw_ot) is None and default_org_type is not None:
            ot = default_org_type
        if ot is None:
            targets.append(Target(excel_row=rownum, sheet=sheet, state_id=sid, dept_id=did,
                                  designation=desig, resolution="unresolved",
                                  error=f"invalid/missing org_type value: {raw_ot!r} (must be "
                                        f"'state' or 'ministry'; pass --default-org-type to fill blanks)"))
            continue
        targets.append(Target(excel_row=rownum, sheet=sheet, state_id=sid, dept_id=did,
                              designation=desig, wing=wing, org_type=ot, names=(sn or "", dn or "")))

    # per-scope: summary availability + existing completed designations
    scopes = {(t.state_id, t.dept_id) for t in targets if t.resolution == "pending"}
    summ_count: dict[tuple, int] = {}
    existing: dict[tuple, set] = {}
    for sc in scopes:
        summ_count[sc] = await _scope_summary_count(user_id, sc[0], sc[1])
        existing[sc] = await _existing_completed(user_id, sc[0], sc[1])

    for t in targets:
        if t.resolution != "pending":
            continue
        sc = (t.state_id, t.dept_id)
        if summ_count.get(sc, 0) == 0:
            t.resolution = "unresolved"
            t.error = "no COMPLETED document summaries in scope (run summary generation first)"
            continue
        t.resolution = "matched"
        t.prior_status = "COMPLETED" if (t.designation or "").lower() in existing.get(sc, set()) else "NOT_STARTED"
    return targets


def build_batches(to_process: list[Target], group_size: int) -> list[Batch]:
    """Group by (state, dept, org_type), then chunk each group into batches of group_size.

    Batches never span groups: the prompt carries one department's document summaries, so a
    mixed batch would generate against the wrong context. A group of 25 costs one call; a group
    of 322 costs 11.
    """
    groups: "OrderedDict[tuple, list]" = OrderedDict()
    for t in to_process:
        groups.setdefault((t.state_id, t.dept_id, t.org_type), []).append(t)

    batches: list[Batch] = []
    n = 0
    for (sid, did, ot), items in groups.items():
        chunks = [items[i:i + group_size] for i in range(0, len(items), group_size)]
        for j, chunk in enumerate(chunks, start=1):
            n += 1
            b = Batch(no=n, state_id=sid, dept_id=did, org_type=ot, names=chunk[0].names,
                      targets=chunk, group_index=j, group_total=len(chunks))
            for t in chunk:
                t.batch_no, t.batch_size = n, len(chunk)
            batches.append(b)
    return batches


# ── token capture ────────────────────────────────────────────────────────────
def _install_token_logging() -> None:
    try:
        from google.genai.models import AsyncModels
    except Exception as exc:
        log.warning(f"token logging unavailable: {exc}")
        return
    if getattr(AsyncModels, "_rm_tok_patched", False):
        return
    _orig = AsyncModels.generate_content

    async def _wrapped(self, **kwargs):
        resp = await _orig(self, **kwargs)
        try:
            um = getattr(resp, "usage_metadata", None)
            sink = _usage_sink.get()
            if um is not None and sink is not None:
                g = lambda a: int(getattr(um, a, 0) or 0)
                for k, a in (("input", "prompt_token_count"), ("output", "candidates_token_count"),
                             ("thinking", "thoughts_token_count"), ("total", "total_token_count")):
                    sink[k] = sink.get(k, 0) + g(a)
        except Exception:
            pass
        return resp

    AsyncModels.generate_content = _wrapped
    AsyncModels._rm_tok_patched = True


# ── execution ────────────────────────────────────────────────────────────────
def _match_generated(batch: Batch, generated: list) -> tuple[dict, list]:
    """Pair each generated mapping back to the Target it was asked for.

    The model returns designation_name as it saw fit, so match case-insensitively on the name and
    fall back to positional order for anything left over. Returns (target -> mapping, leftovers).
    """
    by_name = {}
    for t in batch.targets:
        by_name.setdefault((t.designation or "").strip().lower(), []).append(t)
    paired: dict = {}
    unmatched_gen = []
    for g in generated:
        name = (g.get("designation_name") or "").strip().lower()
        bucket = by_name.get(name)
        if bucket:
            paired[id(bucket.pop(0))] = g
            if not bucket:
                by_name.pop(name, None)
        else:
            unmatched_gen.append(g)
    # positional fallback for any the model renamed
    leftover_targets = [t for t in batch.targets if id(t) not in paired]
    for t, g in zip(leftover_targets, unmatched_gen):
        paired[id(t)] = g
    return paired, [t for t in batch.targets if id(t) not in paired]


# Transport-level failures: the socket dies before any HTTP response exists, so there is no
# status code. settings.GEMINI_RETRY_HTTP_STATUS_CODES ([429, 500, 502, 503, 504]) therefore
# never fires for these -- the SDK's own retry is blind to them and gives up immediately.
# Sending several ~157K-token prompts at once is exactly what provokes them, so this script
# has to recognise and retry them itself.
_CONNECTION_ERROR_MARKERS = (
    "connection reset", "connectionreseterror", "clientoserror", "errno 54",
    "server disconnected", "connection aborted", "connection closed",
    "broken pipe", "errno 32", "cannot connect", "connectionerror",
    "serverdisconnected", "incompleteread", "remote end closed",
)


def _is_connection_error(err: Optional[str]) -> bool:
    """Whether a recorded error string looks like a dropped connection rather than a refusal."""
    e = (err or "").lower()
    return any(m in e for m in _CONNECTION_ERROR_MARKERS)


def _retry_delay(attempt: int, connection_error: bool = False) -> float:
    """Exponential backoff with full jitter.

    Jitter matters more than the backoff here: when N batches are reset by the same overload
    they all fail at once, and a fixed delay would march them back into the wire together.
    Randomising the wait spreads the retries out. A connection error starts from a longer base
    because it means the pipe was saturated, not that the model refused.
    """
    base = RETRY_BACKOFF_CONNECTION if connection_error else RETRY_BACKOFF_BASE
    ceiling = min(base * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
    return random.uniform(base, ceiling) if ceiling > base else base


async def match_igot(done: list, matcher, label: str) -> int:
    """Match saved designations against the iGOT master and store the winners.

    Mirrors the v3 API's post-generation auto-match (src/api/v3/role_mappings.py):
      * match on the STORED designation_name -- the model may rename a designation, and the
        stored name is what a reviewer sees, so matching the input name would attach the id to
        the wrong label;
      * dedup the names before calling -- the matcher hits the KB search API and (on the
        semantic path) an embedding model, so duplicates cost real requests;
      * one bulk_update_designation_matching() call instead of an update per row.

    The matcher returns ONLY the names it matched, so anything absent simply stays unmatched --
    that is a legitimate outcome, not an error. Non-critical throughout: a failure here leaves
    the generated mappings intact and unmatched, exactly as the API does.
    """
    if not done:
        return 0
    try:
        # stored name -> the targets carrying it (several designations can share a name)
        by_name: dict = {}
        for t in done:
            by_name.setdefault((t.stored_name or t.designation or "").strip(), []).append(t)
        names = [n for n in by_name if n]
        if not names:
            return 0

        async with sessionmanager.session() as db:
            results = await matcher.match(db, names)
        match_dict = {(m.get("input_designation") or "").lower(): m
                      for m in (results or []) if m.get("id")}

        bulk_updates = []
        for name, targets in by_name.items():
            m = match_dict.get(name.lower())
            if not m:
                continue
            for t in targets:
                t.igot_designation_name = m.get("designation")
                t.igot_designation_id = m.get("id")
                t.igot_match_type = m.get("match_type") or ""
                bulk_updates.append({
                    "role_mapping_id": uuid.UUID(t.role_mapping_id),
                    "igot_designation_name": m.get("designation"),
                    "igot_designation_id": m.get("id"),
                })

        updated = 0
        if bulk_updates:
            async with sessionmanager.session() as db:
                updated = await crud_role_mapping.bulk_update_designation_matching(db, bulk_updates)
        kinds = {}
        for t in done:
            if t.igot_designation_id:
                kinds[t.igot_match_type or "?"] = kinds.get(t.igot_match_type or "?", 0) + 1
        log.info(f"  iGOT match [{label}]: {updated}/{len(done)} matched"
                 + (f" {kinds}" if kinds else "")
                 + (f", {len(names)} unique name(s) looked up" if len(names) != len(done) else ""))
        return updated
    except Exception as e:
        log.warning(f"iGOT match failed for {label} (non-critical): {e}")
        return 0


async def process_batch(b: Batch, svc, matcher, user_id: uuid.UUID, instruction: Optional[str],
                        force: bool, retries: int, counter: dict, total: int,
                        report: "Report", annotator: "Optional[SourceAnnotator]"):
    """PASS 2 (FRAC) for a whole batch in ONE call, then PASS 4 + save per designation."""
    sink: dict = {}
    tok_ctx = _usage_sink.set(sink)
    generated: list = []
    err: Optional[str] = None
    attempts = 0
    try:
        summary = await _get_scope_summary(user_id, b.state_id, b.dept_id) or "N/A"
        org_data = {
            "org_type": b.org_type.value,
            "state_center_id": b.state_id,
            "department_id": b.dept_id,
            "organization_name": b.names[0] or b.state_id,
            "department_name": b.names[1] or "N/A",
            "docs_summary": summary if summary else "N/A",
            "instruction": instruction or "N/A",
        }
        # the designations FROM THE FILE -- this is what replaces PASS 1
        payload = [{"designation": t.designation,
                    "sort_order": i,
                    "wing_division_section": t.wing or "N/A"}
                   for i, t in enumerate(b.targets, start=1)]
        for attempt in range(1, retries + 2):
            attempts = attempt
            try:
                res = await asyncio.wait_for(
                    svc._generate_frac_for_batch(payload, org_data, batch_number=b.no),
                    timeout=PER_BATCH_TIMEOUT)
                if res:
                    generated = svc.reconcile_role_mappings_with_kcm(res)   # PASS 4 (sync)
                    err = None
                    break
                err = "empty FRAC response from model"
            except asyncio.TimeoutError:
                err = f"timeout >{PER_BATCH_TIMEOUT}s"
            except Exception as e:
                err = f"{type(e).__name__}: {e}".strip().rstrip(":")
                log.warning(f"[batch {b.no}] {b.state_id}/{b.dept_id or '-'} error "
                            f"(attempt {attempt}): {err}")
            # Back off before re-sending. A retry re-uploads the whole ~157K-token prompt, so a
            # lockstep retry of several batches can reproduce the very collision that failed
            # them -- the jitter is what pulls them apart. Connection resets get a longer wait
            # than a model-side failure because they mean the pipe itself is saturated.
            if attempt <= retries:
                delay = _retry_delay(attempt, connection_error=_is_connection_error(err))
                log.info(f"[batch {b.no}] retrying in {delay:.1f}s "
                         f"(attempt {attempt + 1}/{retries + 1})")
                await asyncio.sleep(delay)
    finally:
        _usage_sink.reset(tok_ctx)

    # Token cost is per CALL; attribute it to the batch and split evenly for per-row reporting.
    tin, tout = sink.get("input", 0), sink.get("output", 0)
    tthink, ttot = sink.get("thinking", 0), sink.get("total", 0)
    n = max(len(b.targets), 1)

    paired, missing = _match_generated(b, generated) if generated else ({}, list(b.targets))

    if force and paired:
        await _delete_existing(user_id, b.state_id, b.dept_id,
                               [t.designation for t in b.targets if id(t) in paired])

    # Save each generated mapping. A partial response still saves what came back -- the
    # designations the model omitted are failed individually below.
    to_save, save_targets = [], []
    for t in b.targets:
        m = paired.get(id(t))
        if m is None:
            continue
        to_save.append(RoleMapping(
            user_id=user_id, org_type=t.org_type.value,
            state_center_id=t.state_id, department_id=t.dept_id or None,
            state_center_name=t.names[0] or None, department_name=t.names[1] or None,
            instruction=instruction, status=ProcessingStatus.COMPLETED,
            designation_name=m.get("designation_name") or t.designation,
            wing_division_section=m.get("wing_division_section") or t.wing,
            role_responsibilities=m.get("role_responsibilities") or [],
            activities=m.get("activities") or [],
            competencies=m.get("competencies") or [],
            sort_order=m.get("sort_order"),
        ))
        save_targets.append((t, m))

    if to_save:
        try:
            created = await crud_role_mapping.create(to_save)
            for (t, m), row in zip(save_targets, created):
                t.final_status = "COMPLETED"
                t.role_mapping_id = str(row.id)
                t.stored_name = row.designation_name     # what iGOT matching runs against
                t.comp_count = len(m.get("competencies") or [])
        except Exception as e:
            log.exception(f"save failed for batch {b.no} ({b.state_id}/{b.dept_id or '-'})")
            for t, _m in save_targets:
                t.final_status, t.error = "FAILED", f"save failed: {e}"

    for t in missing:
        t.final_status = "FAILED"
        t.error = err or "designation missing from the model's batch response"

    # iGOT matching, once for the whole batch rather than per designation.
    if matcher is not None:
        done = [t for t, _ in save_targets if t.final_status == "COMPLETED"]
        if done:
            await match_igot(done, matcher, f"batch {b.no}")

    ok = sum(1 for t in b.targets if t.final_status == "COMPLETED")
    for t in b.targets:
        t.attempts = attempts
        t.tok_input, t.tok_output = tin // n, tout // n
        t.tok_thinking, t.tok_total = tthink // n, ttot // n
        await report.add(t)

    counter["done"] += 1
    toks = f"  tokens[in={tin} out={tout} think={tthink} total={ttot}]" if ttot else ""
    log.info(f"  ({counter['done']}/{total}) batch {b.no} "
             f"{b.state_id}/{b.dept_id or '-'} [{b.group_index}/{b.group_total}] :: "
             f"{len(b.targets)} designations -> {ok} COMPLETED"
             + (f", {len(b.targets) - ok} FAILED" if ok < len(b.targets) else "")
             + (f" [x{attempts}]" if attempts > 1 else "") + toks
             + (f"  ERROR: {err}" if err else ""))
    if annotator is not None:
        await annotator.maybe_write()


async def run_execute(batches: list[Batch], user_id: uuid.UUID, instruction: Optional[str],
                      force: bool, igot_match: bool, concurrency: int, retries: int,
                      report: "Report", annotator: "Optional[SourceAnnotator]",
                      stagger: float = BATCH_START_STAGGER):
    from src.services.v3.role_mapping_service import role_mapping_service as svc
    matcher = None
    if igot_match:
        from src.services.designation_matcher_service import designation_matcher_service as matcher
    _install_token_logging()
    sem = asyncio.Semaphore(concurrency)
    counter = {"done": 0}
    total = len(batches)
    # Serialises the moment each call goes onto the wire. The semaphore caps how many run at
    # once but releases them all on the same tick, so without this the first `concurrency`
    # batches upload ~157K tokens each simultaneously -- the cause of the reset storms.
    launch_lock = asyncio.Lock()

    async def _guarded(b: Batch):
        async with sem:
            if stagger > 0:
                async with launch_lock:
                    await asyncio.sleep(stagger)
            await process_batch(b, svc, matcher, user_id, instruction, force, retries,
                                counter, total, report, annotator)

    await asyncio.gather(*[_guarded(b) for b in batches], return_exceptions=True)


# ── report + per-row status ──────────────────────────────────────────────────
_FIELDS = ["sheet", "row", "resolution", "state_center_id", "department_id", "designation",
           "wing_division_section", "org_type",
           "state_center_name", "department_name", "batch_no", "batch_size", "prior_status",
           "final_status", "attempts", "competencies", "role_mapping_id", "igot_designation_name",
           "igot_designation_id", "igot_match_type", "tok_input", "tok_output", "tok_thinking",
           "tok_total", "error"]


def _row_of(t: Target) -> dict:
    return {"sheet": t.sheet or "", "row": t.excel_row, "resolution": t.resolution,
            "state_center_id": t.state_id or "", "department_id": t.dept_id or "",
            "designation": t.designation or "", "wing_division_section": t.wing or "",
            "org_type": t.org_type.value if t.org_type else "",
            "state_center_name": t.names[0], "department_name": t.names[1],
            "batch_no": t.batch_no or "", "batch_size": t.batch_size or "",
            "prior_status": t.prior_status or "", "final_status": t.final_status or "",
            "attempts": t.attempts or "", "competencies": t.comp_count or "",
            "role_mapping_id": t.role_mapping_id or "",
            "igot_designation_name": t.igot_designation_name or "",
            "igot_designation_id": t.igot_designation_id or "",
            "igot_match_type": t.igot_match_type or "",
            "tok_input": t.tok_input or "", "tok_output": t.tok_output or "",
            "tok_thinking": t.tok_thinking or "", "tok_total": t.tok_total or "",
            "error": t.error or ""}


class Report:
    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=_FIELDS)
        self._w.writeheader()
        self._f.flush()
        self._lock = asyncio.Lock()

    def add_sync(self, t: Target):
        self._w.writerow(_row_of(t))
        self._f.flush()

    async def add(self, t: Target):
        async with self._lock:
            self.add_sync(t)

    def close(self):
        self._f.close()


def write_report(path: str, targets: list[Target]):
    r = Report(path)
    for t in targets:
        r.add_sync(t)
    r.close()
    log.info(f"report written: {path}")


_STATUS_COLS = ["run_status", "run_batch_no", "run_competencies", "run_role_mapping_id",
                "run_tokens", "run_error", "run_updated_at"]


class SourceAnnotator:
    """Per-row status written back into the source CSV (in place for a .csv; sibling for .xlsx),
    so the input doubles as a progress tracker. One Target per (state, dept, designation), so
    each source row maps to exactly one Target (by sheet+row)."""
    def __init__(self, path, headers, rows, targets, cols, include_origin):
        self.path = path
        self.headers = [h for h in headers if h not in _STATUS_COLS and h not in ("sheet", "row")]
        self.rows = rows
        self.cols = cols
        self.include_origin = include_origin
        self._n = 0
        self._lock = asyncio.Lock()
        self.by_rowkey: dict[tuple, Target] = {}
        for t in targets:
            self.by_rowkey[(t.sheet, t.excel_row)] = t

    def _agg(self, r: dict) -> dict:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = {c: "" for c in _STATUS_COLS}
        base["run_updated_at"] = now
        t = self.by_rowkey.get((r.get(_SHEET_KEY, ""), r.get(_ROW_KEY)))
        if t is None:
            base["run_status"] = "SKIPPED"
            return base
        if t.resolution != "matched":
            base["run_status"] = t.resolution.upper()
            base["run_error"] = t.error or ""
            return base
        base["run_status"] = t.final_status or "PENDING"
        base["run_batch_no"] = t.batch_no or ""
        base["run_competencies"] = t.comp_count or ""
        base["run_role_mapping_id"] = t.role_mapping_id or ""
        base["run_tokens"] = t.tok_total or ""
        base["run_error"] = t.error or ""
        return base

    def write(self):
        cols = (["sheet", "row"] if self.include_origin else []) + self.headers + _STATUS_COLS
        tmp = self.path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in self.rows:
                row = {h: (r.get(h) if r.get(h) is not None else "") for h in self.headers}
                if self.include_origin:
                    row["sheet"], row["row"] = r.get(_SHEET_KEY, ""), r.get(_ROW_KEY, "")
                row.update(self._agg(r))
                w.writerow(row)
        os.replace(tmp, self.path)

    async def maybe_write(self, every: int = 1):
        """Rewritten after every batch -- batches are few and slow, so per-batch is cheap."""
        async with self._lock:
            self._n += 1
            if self._n % every == 0:
                self.write()


def _tally(items, attr):
    out: dict[str, int] = {}
    for it in items:
        out[getattr(it, attr) or "-"] = out.get(getattr(it, attr) or "-", 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


async def main():
    ap = argparse.ArgumentParser(
        description="Bulk GROUP-WISE role-mapping runner (dev): one LLM call per batch of "
                    "designations within a (state, department) group.")
    ap.add_argument("--excel", default=EXCEL_PATH, required=not EXCEL_PATH,
                    help="source file: .xlsx (all tabs) or .csv")
    ap.add_argument("--sheet", default=EXCEL_SHEET, help="xlsx: restrict to one tab (default: all)")
    ap.add_argument("--user-id", required=True, type=uuid.UUID,
                    help="UUID to attribute generated role mappings to. Mandatory.")
    ap.add_argument("--instruction", default="", help="optional extra instruction for generation")
    # On by default, matching the v3 API, which auto-matches after every generation.
    # --no-igot-match turns it off (e.g. to avoid the KB search calls during a bulk run).
    ap.add_argument("--igot-match", dest="igot_match", action="store_true", default=True,
                    help="match designations to the iGOT master (default: on)")
    ap.add_argument("--no-igot-match", dest="igot_match", action="store_false",
                    help="skip iGOT designation matching")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--execute", action="store_true", help="actually generate (default: dry run)")
    ap.add_argument("--group-size", type=int, default=GROUP_SIZE,
                    help=f"designations per LLM call (default {GROUP_SIZE})")
    ap.add_argument("--limit-batches", type=int, default=0,
                    help="process at most N batches (0 = all) -- for smoke tests")
    ap.add_argument("--limit", type=int, default=0,
                    help="consider at most N designations before batching (0 = all)")
    ap.add_argument("--force", action="store_true", help="regenerate even if a mapping already exists")
    ap.add_argument("--concurrency", type=int, default=CONCURRENT_BATCHES,
                    help=f"batch-calls in flight at once (default {CONCURRENT_BATCHES}); each "
                         f"carries the full ~157K-token prompt")
    ap.add_argument("--retries", type=int, default=RETRIES,
                    help=f"retries per batch after the first attempt (default {RETRIES}); "
                         f"connection resets back off longer than model-side failures")
    ap.add_argument("--stagger", type=float, default=BATCH_START_STAGGER,
                    help=f"seconds between batch starts (default {BATCH_START_STAGGER}); stops "
                         f"concurrent calls putting their ~157K-token prompts on the wire at "
                         f"the same instant. 0 disables.")
    ap.add_argument("--default-org-type", default="", choices=["", "state", "ministry"],
                    help="org_type to use where the column is EMPTY (the prod export is blank on "
                         "most rows). A present-but-invalid value is still an error.")
    ap.add_argument("--state-col", default="")
    ap.add_argument("--dept-col", default="")
    ap.add_argument("--designation-col", default="")
    ap.add_argument("--log-file", default=DEFAULT_LOG_FILE)
    ap.add_argument("--status-out", default="")
    ap.add_argument("--no-annotate", action="store_true")
    args = ap.parse_args()

    if args.group_size < 1:
        raise SystemExit("[config] --group-size must be >= 1")

    log_path = setup_logging(args.log_file)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.info("=" * 78)
    log.info(f"RUN {started} | mode={'EXECUTE' if args.execute else 'DRY-RUN'} | source={args.excel} "
             f"| user_id={args.user_id} | group_size={args.group_size} "
             f"| concurrency={args.concurrency} | igot_match={args.igot_match}")
    log.info(f"logging to: {log_path}")

    user_uuid: uuid.UUID = args.user_id
    default_ot = _org_type_of(args.default_org_type) if args.default_org_type else None

    sessionmanager.init(settings.DATABASE_URL)
    log.info(f"DB: {str(settings.DATABASE_URL).split('@')[-1]}")

    headers, rows = load_rows(args.excel, args.sheet)
    log.info(f"source: {len(rows)} data rows")

    state_col = detect_col(headers, args.state_col, "state")
    dept_col = detect_col(headers, args.dept_col, "dept")
    desig_col = detect_col(headers, args.designation_col, "designation")
    statename_col = detect_col(headers, "", "statename")
    deptname_col = detect_col(headers, "", "deptname")
    orgtype_col = detect_col(headers, "", "orgtype")
    wing_col = detect_col(headers, "", "wing")
    log.info("detected columns -> " + ", ".join(f"{k}={v!r}" for k, v in [
        ("state", state_col), ("dept", dept_col), ("designation", desig_col), ("org_type", orgtype_col),
        ("state_name", statename_col), ("dept_name", deptname_col), ("wing", wing_col)]))

    missing = [label for col, label in [
        (state_col, "state_center_id"), (dept_col, "department_id"), (orgtype_col, "org_type"),
        (statename_col, "state_center_name"), (deptname_col, "department_name"),
        (desig_col, "designation"),
    ] if not col]
    if missing:
        raise SystemExit(f"[config] source file is missing required column(s): {missing}. "
                         f"Headers found: {headers}")

    filtered = rows
    if not filtered:
        raise SystemExit("[config] source file has no data rows.")

    # id numeric-corruption guard
    for col, label in [(state_col, "state"), (dept_col, "dept")]:
        vals = [_s(r.get(col)) for r in filtered if _s(r.get(col))]
        bad = [v for v in vals if re.search(r"[eE]\+?\d|\.\d", v)]
        if bad:
            log.warning(f"!! {label} id column {col!r}: {len(bad)}/{len(vals)} values look NUMERIC "
                        f"(e.g. {bad[0]!r}) -- long ids likely CORRUPTED by Excel; matches will fail. "
                        f"Format the column as TEXT or supply a CSV.")

    cols = {"state": state_col, "dept": dept_col, "designation": desig_col,
            "statename": statename_col, "deptname": deptname_col, "orgtype": orgtype_col,
            "wing": wing_col}
    targets = await resolve_targets(filtered, cols, user_uuid, default_ot)

    log.info(f"resolution: {_tally(targets, 'resolution')}")
    matched = [t for t in targets if t.resolution == "matched"]
    log.info(f"matched by prior status: {_tally(matched, 'prior_status')}")

    to_process = [t for t in matched
                  if t.prior_status == "NOT_STARTED" or (args.force and t.prior_status == "COMPLETED")]
    if args.limit:
        to_process = to_process[:args.limit]

    batches = build_batches(to_process, args.group_size)
    if args.limit_batches and len(batches) > args.limit_batches:
        dropped = batches[args.limit_batches:]
        batches = batches[:args.limit_batches]
        keep = {id(t) for b in batches for t in b.targets}
        for b in dropped:
            for t in b.targets:
                t.batch_no = t.batch_size = 0
        to_process = [t for t in to_process if id(t) in keep]

    groups = len({(b.state_id, b.dept_id) for b in batches})
    log.info(f"TO PROCESS: {len(to_process)} designations -> {groups} groups -> {len(batches)} "
             f"LLM calls (group_size={args.group_size}, force={args.force})")
    if batches:
        sizes = [len(b.targets) for b in batches]
        full = sum(1 for s in sizes if s == args.group_size)
        log.info(f"batch sizes: min={min(sizes)} max={max(sizes)} avg={sum(sizes)/len(sizes):.1f} "
                 f"| full batches: {full}/{len(batches)} "
                 f"| partial-batch overhead: {len(batches) - -(-len(to_process) // args.group_size)} "
                 f"extra call(s) vs. perfect packing")

    annotator = None
    if not args.no_annotate:
        is_csv = args.excel.lower().endswith(".csv")
        status_out = args.status_out or (args.excel if is_csv
                                         else str(Path(args.excel).with_suffix("")) + ".rolemap_status.csv")
        annotator = SourceAnnotator(status_out, headers, rows, targets, cols, include_origin=not is_csv)
        log.info(f"per-row status -> {status_out}")

    out = args.out or str(Path(args.excel).with_name(
        f"role_mapping_groupwise_{'run' if args.execute else 'plan'}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"))

    if not args.execute:
        for b in batches[:12]:
            log.info(f"  batch {b.no}: {b.names[1][:44] or b.dept_id or '-'} "
                     f"[{b.group_index}/{b.group_total}] -> {len(b.targets)} designations")
        if len(batches) > 12:
            log.info(f"  ... and {len(batches) - 12} more batches")
        write_report(out, targets)
        if annotator is not None:
            annotator.write()
            log.info(f"per-row status written: {annotator.path}")
        log.info("DRY RUN complete -- no role mappings generated. Re-run with --execute to process.")
        await sessionmanager.close()
        return

    report = Report(out)
    proc_ids = {id(t) for t in to_process}
    for t in targets:
        if id(t) not in proc_ids:
            t.final_status = t.final_status or t.prior_status
            report.add_sync(t)
    if annotator is not None:
        annotator.write()
    log.info(f"EXECUTING {len(batches)} batch calls (PASS 2 + PASS 4), {args.concurrency} at a time "
             f"(retries={args.retries}, stagger={args.stagger}s, "
             f"per-batch timeout={PER_BATCH_TIMEOUT}s) ...")
    try:
        await run_execute(batches, user_uuid, args.instruction or None, args.force,
                          args.igot_match, args.concurrency, args.retries, report, annotator,
                          stagger=args.stagger)
    finally:
        report.close()
        if annotator is not None:
            annotator.write()
    log.info(f"final status: {_tally(to_process, 'final_status')}")
    ttot = sum(t.tok_total for t in to_process)
    log.info(f"TOTAL tokens over {len(batches)} calls / {len(to_process)} designations: "
             f"input={sum(t.tok_input for t in to_process)} output={sum(t.tok_output for t in to_process)} "
             f"thinking={sum(t.tok_thinking for t in to_process)} total={ttot}")
    log.info(f"report: {out}")
    if annotator is not None:
        log.info(f"per-row status: {annotator.path}")
    await sessionmanager.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)

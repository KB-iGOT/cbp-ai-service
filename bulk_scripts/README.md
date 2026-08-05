# Final Backend Execution Plan for AI CBP Course Recommendation & Publishing on iGOT Platform

## Description

This document is the end-to-end backend execution plan for generating AI-driven Capacity Building
Plans (CBP) — course recommendations, approval, and publishing — for designations across
states/ministries and departments onto the iGOT platform.

The plan is executed via a sequence of standalone backend scripts in this directory, each covering
one stage of the pipeline: source document ingestion, document summarization, role mapping
generation, course recommendation + CBP plan generation, approval-request submission, and final
publishing of the approved Training Plan to iGOT. Each stage is dry-run by default, writes an
outcome CSV + log per run, and (with one noted exception) is safe to re-run without duplicating
work — so the pipeline can be run incrementally, stage by stage, verified at each step before
moving to the next.

**Execution stages** (in order):

| # | Stage | Script | Purpose |
|---|---|---|---|
| 1 | Document ingestion | `batch_copy_all_documents.py` | Copy source documents onto the target user so downstream stages have a consistent document set to work from. |
| 2 | Document summarization | `batch_document_summary.py` | Generate an LLM summary for each ingested document — the grounding context for role mapping generation in stage 3. |
| 3 | Role mapping generation | `batch_rolemapping_generate.py` | For each designation, generate a v3 role mapping (roles & responsibilities, activities, competencies) from the document summaries produced in stage 2. |
| 4 | Course recommendation + CBP plan generation | `batch_generate_and_save_cbp_plan.py` | For each role mapping, run hybrid vector search + LLM filtering to recommend courses, then save the CBP plan. |
| 5 | Approval request submission | `batch_send_approval_requests.py` | Submit each generated CBP plan as an approval request (one request per designation) and notify the approving MDO. |
| 6 | Publishing to iGOT | `bulk_training_plan_approval.py` | Once approved, publish the Training Plan to iGOT via the CB ext course service's AICBP create/publish APIs. |

Full usage details for each script — CLI flags, required environment variables, input file column
requirements, output file locations, and example commands — follow below.

---

# Bulk Scripts — Usage Guide

Standalone Python scripts for bulk/batch operations against the shared Postgres DB (and, where
needed, GCS/Gemini/iGOT). Each script is self-contained and run manually from the repo root.

## Repo setup

**Repository**: [github.com/KB-iGOT/cbp-ai-service](https://github.com/KB-iGOT/cbp-ai-service/tree/cbrelease-4.8.39) (branch `cbrelease-4.8.39`)

```bash
# From the repo root
git clone https://github.com/KB-iGOT/cbp-ai-service.git
cd cbp-ai-service
git checkout cbrelease-4.8.39

# Install uv if you don't have it: https://docs.astral.sh/uv/getting-started/installation/

# Create the virtualenv and install all dependencies (from pyproject.toml / uv.lock)
uv sync

# Activate the virtualenv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate        # Windows

# Copy the env template and fill in real values
cp .env.example .env
```

Fill in `.env` with the values your team uses for the DB, GCS, Gemini/Vertex, and the other
per-script service URLs/tokens listed below. Never commit real secrets — `.env` is git-ignored.

<span style="color:red">**Copy ALL environment variables from the `cbp-ai-service` app's own `.env` — match the environment
you're running these scripts against: if you're running in dev, copy ALL dev values; if you're
running in prod, copy ALL prod values. Never mix values from different environments in one
`.env` file.**</span>

Playwright (used by some parts of the app, not the bulk scripts) needs its browser binaries once:
```bash
uv run playwright install --with-deps chromium
```

Every command below assumes you're in the repo root with the virtualenv activated (see above). Two
scripts (`batch_document_summary.py`, `batch_rolemapping_generate.py`) import from `src/` and
require the project's dependencies to be installed in the active interpreter; the other four are
fully standalone.

## Common conventions

- **Dry-run by default.** Every script defaults to a dry run (no DB writes, no external calls that
  cost money or mutate anything) and only opts into real changes when you pass `--execute`. Always
  run without `--execute` first and read the output before running for real.
- **Config comes from `.env`, not CLI flags.** Database URLs, service URLs, tokens, and credentials
  are read from the repo's `.env` file (or the shell environment) — never passed on the command
  line. Only per-run inputs (excel path, user id, batch size, etc.) are CLI flags.
- **Every mandatory env var is enforced.** If something required is missing, the script exits
  immediately with a clear message naming the missing variable — it will never silently proceed
  with a wrong default.
- **Outcome CSV + log file, every run.** Each script writes one outcome CSV (one row per unit of
  work — dry-run or `--execute`) plus a timestamped log file. Read the CSV first; it's the fastest
  way to see what happened.
- **`--batch-size` controls concurrency**, not correctness — raising it processes more rows in
  parallel; it doesn't change what gets processed. Default is `10` everywhere it applies.

## Before you run anything

1. Make sure `.env` exists at the repo root and has the variables listed for the script you're
   running (see each section below).
2. Pick the **user id** (UUID) whose data you're operating on/as — every script takes `--user-id`.
3. Do a **dry run first**. Read the outcome CSV and the console/log output. Only add `--execute`
   once the dry-run plan looks right.

---

## 1. `batch_copy_all_documents.py`

**What it does**: Copies every document row in the `documents` table (excluding ones already owned
by the target user) to one target user — duplicates both the GCS file and the DB row. No input
file; it acts on the whole table directly.

**What it handles**: Deduplicates source documents by `(state_center_id, department_id, filename)`
before copying, keeping only the most recently created one per group (an empty/NULL
`department_id` is its own consistent group). A document with no real file in GCS is reported
`not_found_in_storage`, not treated as fatal — the rest of the batch continues. Every copy always
gets a brand-new DB row and a brand-new GCS object path, so it never overwrites or collides with
anything from a previous run.

**Env required**: `DATABASE_URL`, `GCP_STORAGE_BUCKET`, `GCP_STORAGE_PREFIX`, `GCP_STORAGE_CREDENTIALS`, `DOCUMENT_STORAGE_TYPE`

Keep these as-is:
```
GCP_STORAGE_PREFIX="documents"
DOCUMENT_STORAGE_TYPE=gcp
```

**Input**: none (reads the `documents` table directly).

**Output**: `bulk_scripts/logs/copy_all_documents_<timestamp>.{log,csv}` — one CSV row per document
(`status, source_file_id, filename, state_center_id, department_id, source_stored_path,
target_user_id, new_stored_path, new_file_id, error`).

**Things to know**
- This runs against the **entire** `documents` table — there's no way to limit it to one state or
  department. A full run touches every document in the system (minus ones the target already owns).
- If the same file was uploaded by different people for the same state/department, only the most
  recently uploaded copy is kept for copying — the rest are silently dropped, not duplicated.
- A copied document does **not** carry over its original AI summary — every copy starts fresh and
  will need its summary regenerated (via script 2) before it can be used downstream.
- Copies are never overwritten — running this again for the same user creates **additional**
  duplicate copies each time, it does not skip what was already copied. Don't re-run against the
  same target user without a reason.
- There's no automatic retry for a document that fails to copy (e.g. a network blip) — it's simply
  logged as failed for that run; you'd need to re-run to pick it up again.
- A document with no file in cloud storage is reported and skipped, not treated as an error — it
  doesn't stop the rest of the batch.

```bash
# 1. Dry run
python bulk_scripts/batch_copy_all_documents.py --user-id <uuid>

# 2. Execute
python bulk_scripts/batch_copy_all_documents.py --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_copy_all_documents.py --user-id <uuid> --execute --batch-size 20
```

---

## 1b. `batch_copy_documents_and_summary.py` (optional utility, not a numbered pipeline stage)

**What it does**: Different from script 1 above — this one copies documents **between two specific
state/department scopes**, driven by an input file, and it carries the **existing AI summary over
as-is** (no resummarization needed afterwards). Use it when a state/department has no documents of
its own yet, and you want to seed it by copying an already-summarized set of documents from another
state/department that already has them — instead of running the full ingestion + summarization
pipeline (scripts 1 + 2) from scratch for that scope.

**What it handles**: Input rows give one scope-pair each: `source_state_center_id,
source_department_id, target_state_center_id, target_department_id`. For each row, every document
in the source scope (regardless of who uploaded it) is a candidate to copy into the target scope,
owned by `--user-id`. Two independent skip checks keep re-runs safe and avoid duplicates:
- **Within the source scope**: documents sharing the same filename are deduped, keeping only the
  most recently created one (`status=` reflected via the dedup log line, not its own CSV row).
- **Against the target scope, for `--user-id` specifically**: a source document is skipped entirely
  (`status=skipped_already_in_target`) if a document with that same filename already exists in the
  target scope **owned by `--user-id`** — so re-running the same input file, or two rows pointing
  at the same target scope for the same user, never creates duplicate copies for that user. A
  same-named document owned by a different user at that target scope does not trigger the skip.

A scope-pair row whose source scope has zero documents is reported `status=no_documents_in_scope`
(not fatal to the rest of the run). A document with no real file in GCS is reported
`not_found_in_storage`, same as script 1.

**Env required**: same as script 1 — `DATABASE_URL`, `GCP_STORAGE_BUCKET`, `GCP_STORAGE_PREFIX`,
`GCP_STORAGE_CREDENTIALS`, `DOCUMENT_STORAGE_TYPE`.

**Input**: `.csv` or `.xlsx`/`.xlsm`. `--sheet` restricts an xlsx read to one tab (default: all tabs).

| Column | Required | Notes |
|---|:---:|---|
| `source_state_center_id` | ✅ | |
| `source_department_id` | | blank = root-level scope |
| `target_state_center_id` | ✅ | |
| `target_department_id` | | blank = root-level scope |
| `source_state_center_name` | | cosmetic only — echoed as-is into the outcome CSV, never looked up (the `documents` table has no name columns) |
| `source_department_name` | | cosmetic only, same as above |
| `target_state_center_name` | | cosmetic only, same as above |
| `target_department_name` | | cosmetic only, same as above |

**Output**: log at `bulk_scripts/logs/<input-file-name>_<timestamp>.log`; outcome CSV written
**alongside the input file** at `<input-file-name>_<timestamp>.csv` (one row per source document
found, plus one row per skipped/empty scope-pair) — columns: `status, sheet, row, source_file_id,
filename, source_state_center_id, source_department_id, source_state_center_name,
source_department_name, target_state_center_id, target_department_id,
target_state_center_name, target_department_name, source_stored_path, source_summary_status,
target_user_id, new_stored_path, new_file_id, error`.

**Things to know**
- Unlike script 1, the copied document's summary (`summary_status`, `summary_text`,
  `summary_error`, `last_summary_request_id`) is carried over unchanged — the copy is immediately
  usable downstream (e.g. role mapping generation) without re-running script 2.
- Source documents are matched by state/department scope only — **not** filtered by uploader, so
  every document under that source scope is a candidate regardless of who originally uploaded it.
- Safe to re-run: a document already present (by filename) in the target scope is skipped, not
  re-copied.
- The underlying GCS object always gets a brand-new UUID-based path (never the original filename)
  so it can never collide in storage — the DB row's `filename` column still keeps the original
  name, so nothing user-visible changes.

```bash
# 1. Dry run
python bulk_scripts/batch_copy_documents_and_summary.py --input <path/to/scopes.csv> --user-id <uuid>

# 2. Execute
python bulk_scripts/batch_copy_documents_and_summary.py --input <path/to/scopes.csv> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_copy_documents_and_summary.py --input <path/to/scopes.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## 2. `batch_document_summary.py`

**What it does**: Bulk-generates document summaries from an Excel/CSV list of documents, calling
the same summary logic the live API uses.

**What it handles**: Auto-detects the filter/yes-no column, the `file_id` (preferred) or `filename`
match column, and the `state`/`dept` scope columns — logs what it detected so you can verify before
trusting a run. Documents already `COMPLETED` are skipped (unless `--force`); a document stuck
`IN_PROGRESS` past `--stale-minutes` (default 30) is treated as crashed from a prior run and
auto-recovered, not left stuck forever. A per-document failure is retried up to `--retries` times
and logged, not fatal to the batch.

**Env required**: `DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY`,
`GOOGLE_PROJECT_ID` (loaded via `src.core.configs.settings`, plus `GCP_STORAGE_*` if
`DOCUMENT_STORAGE_TYPE=gcp`).

**Input**: `.xlsx` (all tabs, or one via `--sheet`) or `.csv`; columns auto-detected as above, or
override with `--filter-col`, `--file-id-col`, `--filename-col`, `--state-col`, `--dept-col`.

**Output**: plan/result CSV next to the source file (`--out` to override); per-row status written
back into the source file (disable with `--no-annotate`); log at
`bulk_scripts/logs/bulk_summary_runner_<timestamp>.log`.

**Things to know**
- The script **rewrites your source file in place** every 10 documents processed (as a live
  progress tracker), unless you pass `--no-annotate`. If you need to keep the original file
  untouched, make a copy before running with `--execute`.
- Column matching is auto-detected from your file's headers — if your spreadsheet has an unexpected
  column name that looks like a match key, the script may pick the wrong matching mode. Always check
  the "detected columns" line in the console output before trusting a run.
- Watch for an Excel gotcha: if a state/department ID looks like it's been mangled into scientific
  notation (e.g. `1.23E+10`) — a common side effect of Excel auto-formatting long numbers — the
  script logs a loud warning, since those rows will likely fail to match anything in the database.
- Each document gets a hard 20-minute processing timeout, and a failed document is automatically
  retried once more in the same run (2 attempts total) before being marked failed.
- A document stuck "in progress" from a crashed prior run is auto-recovered after 30 minutes of
  inactivity (configurable via `--stale-minutes`) — it won't stay stuck forever.
- Dry run makes zero AI calls — no cost is incurred until you pass `--execute`.

```bash
# 1. Dry run
python bulk_scripts/batch_document_summary.py --excel <path/to/file.xlsx> --user-id <uuid>

# 2. Execute
python bulk_scripts/batch_document_summary.py --excel <path/to/file.xlsx> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_document_summary.py --excel <path/to/file.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## 3. `batch_rolemapping_generate.py`

**What it does**: Bulk-generates a v3 role mapping (one designation per row) — runs only the
FRAC-generation and KCM-reconciliation passes, skipping designation extraction.

**What it handles**: Requires document summaries to already exist for a scope (run script 2 first)
— a scope with none is reported `unresolved`, not processed, and does not stop the rest of the
batch. An unparseable `org_type` value marks just that row `unresolved` with an error, not the
whole run. A designation that already has a mapping is skipped by default (re-generate with
`--force`). Optionally matches designations to the iGOT master (`--igot-match`, on by default).

**Env required**: `DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY`,
`GOOGLE_PROJECT_ID` (loaded via `src.core.configs.settings`, same as script 2), plus
`REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, `REDIS_DESIG_EMB_PREFIX` — needed because `--igot-match`
(on by default) caches designation embeddings in Redis via `designation_matcher_service`. These
four have code-level defaults (`localhost`/`6379`/`0`/`cbp_desig_emb`), so the script won't fail
outright if they're unset, but they must point at the correct Redis instance for this environment
or the iGOT-match step will silently talk to the wrong (or no) Redis.

**Input**: `.xlsx`/`.csv`. **ALL** columns below are mandatory — missing any one aborts the whole
run (not just the affected row):

| Column | Required | Notes |
|---|:---:|---|
| `state_center_id` | ✅ | |
| `department_id` | ✅ | blank = root-level scope (still must be present as a column) |
| `org_type` | ✅ | must parse to `state` or `ministry` (tolerant of synonyms like `center`/`central`/`union`) |
| `state_center_name` | ✅ | |
| `department_name` | ✅ | |
| `designation` | ✅ | |

No yes/no filter column exists for this script — every row is processed unless it's a duplicate
scope+designation or its scope has no document summaries yet.

**Output**: plan/result CSV next to source (`--out` to override); per-row status written back to
the source file (disable with `--no-annotate`); log at
`bulk_scripts/logs/role_mapping_runner_<timestamp>.log`.

**Things to know**
- **Hard prerequisite, easy to miss**: this needs document summaries to already exist for each
  state/department (from script 2). A scope with no summaries is reported `unresolved` and skipped
  silently — if you forget to run script 2 first, large parts of your file can end up unprocessed
  without an obvious error.
- This is a scaled-down version of the full mapping pipeline — it only runs 2 of the normal 4
  processing stages (it assumes the designation name is already known and skips domain detection),
  so its output is less deep than the equivalent step in the live product.
- A duplicate row (same state + department + designation, regardless of capitalization) is detected
  and only the first occurrence is processed — the rest are marked `duplicate` and dropped, not
  mapped again.
- Regenerating an existing mapping (`--force`) **deletes the old mapping first** before creating the
  new one — if generation then fails, the old data is already gone.
- Unlike some of the other scripts, there is no yes/no filter column here — every row in the file is
  processed unless it's a duplicate or its scope is unresolved.
- The optional "match to the iGOT master list" step is best-effort: if it fails, the mapping itself
  is still saved successfully; only that enrichment step is skipped and logged as a warning.

```bash
# 1. Dry run
python bulk_scripts/batch_rolemapping_generate.py --excel <path/to/source.csv> --user-id <uuid>

# 2. Execute
python bulk_scripts/batch_rolemapping_generate.py --excel <path/to/source.csv> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_rolemapping_generate.py --excel <path/to/source.csv> --user-id <uuid> --execute --batch-size 20
```

---

## 3b. `copy_role_mapping_by_designation.py` (optional utility, not a numbered pipeline stage)

**What it does**: Copies existing `role_mappings` rows (one designation's FRAC mapping) from a
source scope to a target scope, driven by an input file. Pure DB, no LLM calls — use it to seed a
designation into a new state/department by cloning an already-generated mapping from elsewhere,
instead of running script 3 (which calls the LLM) again for that designation.

**What it handles**: Each input row names one source designation and one target designation (by
scope + name). Source rows are matched by **scope + designation name only** — not filtered by
uploader/owner, so any user's mapping in that source scope is a candidate. If more than one valid
source row matches (e.g. mapped by different users), only the **most recently created** one is used
— the rest are silently ignored (dedup by designation name, keep newest). A source designation is
only copied if it's `COMPLETED` **and** carries real content (non-empty
`role_responsibilities`/`activities`/`competencies`); otherwise the row is skipped as
`source_not_completed` or `source_empty`, not fatal to the rest of the run. A missing source
designation is `source_not_found`. If the **target** user already has that designation in the
target scope, the row is skipped as `skipped_existing` — safe to re-run without duplicating.
`sort_order` for the new row is computed fresh for the target scope (`MAX(sort_order)+1`, or `1` if
the target scope has none yet for that user) — never copied from the source.

**Env required**: `DATABASE_URL` only (pure DB script, no GCS/Gemini).

**Input**: `.csv` (tab- or comma-delimited, auto-detected) or `.xlsx`/`.xlsm`. Also accepts the
display-style "From ... / To ..." headers (case/space-insensitive) — both forms map to the same
columns. The user id is **not** in the file — every copy is created under `--user-id`.

**FROM (source) columns:**

| Column | Required | Notes |
|---|:---:|---|
| `source_state_center_id` | ✅ | |
| `source_state_center_name` | | context only |
| `source_department_id` | | blank = root scope |
| `source_department_name` | | context only |
| `source_designation_id` | | informational — matching is by name, not id |
| `source_designation_name` | ✅ | source designation to copy |
| `source_org_type` | ✅ | must be `state` or `ministry` — context only, never applied to the new row |

**TO (target) columns:**

| Column | Required | Notes |
|---|:---:|---|
| `target_state_center_id` | ✅ | |
| `target_state_center_name` | ✅ | becomes the new row's `state_center_name` |
| `target_department_id` | | blank = root scope |
| `target_department_name` | | becomes the new row's `department_name` |
| `target_designation_id` | | informational |
| `target_designation_name` | ✅ | target designation |
| `target_org_type` | ✅ | must be `state` or `ministry` — becomes the new row's `org_type` |

An ID cell pasted in scientific notation (e.g. `1.36E+18`) is rejected (`id_scientific_notation`)
rather than silently mismatched — format ID columns as Text before pasting. Either org_type column
with an unrecognized value (not `state`/`ministry`, tolerant to a few synonyms like
`center`/`central`/`union`) is rejected as `invalid_org_type`, not fatal to the rest of the run.

**Output**: log at `bulk_scripts/logs/<input-file-name>_<timestamp>.log`; outcome CSV written
**alongside the input file** at `<input-file-name>_<timestamp>.csv` — one row per input line,
columns: `row_no, status, reason, source_state_center_id, source_department_id,
source_designation, source_org_type, source_id, source_status, target_user_id,
target_state_center_id, target_department_id, target_designation, target_org_type,
new_role_mapping_id, new_sort_order`.

**Things to know**
- `--user-id` is used **only** for the target side (ownership of the new row and the
  already-has-this-designation check) — it never filters or restricts the source lookup.
- Only the `role_mappings` row itself is cloned — suggested/user-added courses, course
  recommendations, and CBP plans are **not** copied; those live downstream of script 4 and would
  need to be regenerated separately for the new scope.
- `state_center_name`/`department_name`/`designation_name`/`status`/`user_id`/`state_center_id`/
  `department_id`/`org_type` are all overridden to the target row's values (`status` is always
  forced to `COMPLETED`; `org_type` always comes from the input's `Target Org Type` column, **never**
  cloned from the source — the target scope's org type can legitimately differ from the source's,
  e.g. copying a state-level designation into a ministry); everything else (the FRAC content,
  `igot_designation_name`/`igot_designation_id`, `sector_name`, `instruction`,
  `wing_division_section`) is copied verbatim from the source.
- Safe to re-run: a designation already present for the target user in the target scope is
  skipped, not duplicated or overwritten.

```bash
# 1. Dry run
python bulk_scripts/copy_role_mapping_by_designation.py --input <path/to/mapping.csv> --user-id <uuid>

# 2. Execute
python bulk_scripts/copy_role_mapping_by_designation.py --input <path/to/mapping.csv> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/copy_role_mapping_by_designation.py --input <path/to/mapping.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## 4. `batch_generate_and_save_cbp_plan.py`

**What it does**: Generates course recommendations and saves CBP plans for every
`role_mapping_id` in an input file (Excel or CSV). Fully self-contained — re-implements the
DB/Gemini logic directly, no HTTP calls to the app.

**What it handles**: A row with a missing/invalid `role_mapping_id` is skipped, not fatal.
Idempotency per role_mapping: a `COMPLETED` recommendation with an existing CBP plan is skipped
entirely; a `FAILED` or stale `IN_PROGRESS` recommendation is deleted and regenerated from scratch;
a `COMPLETED` recommendation missing only its CBP plan reuses the existing courses (no LLM
re-call). A role_mapping with neither `igot_designation_name` nor `igot_designation_id` set is
skipped (`SKIPPED_NO_IGOT_DESIGNATION`), since both are mandatory for course recommendation.
Dry-run never calls the LLM/embedding pipeline, even for rows that would need fresh generation.

**Env required**: `DATABASE_URL`, `GOOGLE_PROJECT_ID`, `GOOGLE_PROJECT_LOCATION_GLOBAL`,
`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_API_KEY`,
`GOOGLE_EMBEDDING_MODEL`, `EMBEDDING_OUTPUT_DIMENSIONALITY`, `GEMINI_PRO_MODEL_NAME`

**Input**: `.xlsx`/`.xlsm` or `.csv`.

| Column | Required | Notes |
|---|:---:|---|
| `role_mapping_id` | ✅ | UUID identifying the role_mapping to process |
| `state_center_id` | | optional, echoed into the outcome CSV only |
| `department_id` | | optional, echoed into the outcome CSV only |
| `org_type` | | optional, echoed into the outcome CSV only |
| `state_center_name` | | optional, echoed into the outcome CSV only |
| `department_name` | | optional, echoed into the outcome CSV only |
| `designation` | | optional, echoed into the outcome CSV only |

**Output**: `<dir of --excel>/batch_generate_and_save_cbp_plan_<timestamp>.csv` (input columns +
`recommendation_id`, `status`, `total_courses`, per-stage token counts, `error`); log at
`bulk_scripts/logs/batch_generate_and_save_cbp_plan_<timestamp>.log`.

**Things to know**
- If you edit the input file and re-run, **already-succeeded rows are not reprocessed** — only new
  rows or previously-failed ones are (re)generated. This saves cost but means a row won't be
  refreshed just because you re-ran the script.
- A row that failed or got stuck last time has its old (incomplete) data **permanently deleted**
  before being regenerated from scratch — this is intentional and safe, but it is a destructive step.
- Course recommendations are capped: no more than 8 "Domain", 4 "Functional", and 4 "Behavioural"
  courses are kept per plan, even if the AI found more good matches — the rest are silently dropped
  regardless of how relevant they scored. Only courses scoring 80%+ relevancy are considered at all.
- Transient failures (database or AI call issues) are retried automatically up to 3 times before
  that row is marked failed.
- Very long error messages are cut off at 4,000 characters when saved to the database (the full
  error is still in the log file, just not in the DB record).
- Dry run makes **zero** AI calls for rows that need fresh generation — genuinely free to run as
  many times as needed to preview a plan.

```bash
# 1. Dry run
python bulk_scripts/batch_generate_and_save_cbp_plan.py --excel <path/to/input.xlsx> --user-id <uuid>

# 2. Execute
python bulk_scripts/batch_generate_and_save_cbp_plan.py --excel <path/to/input.xlsx> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_generate_and_save_cbp_plan.py --excel <path/to/input.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## 5. `batch_send_approval_requests.py`

**What it does**: Submits bulk approval requests for CBP plans (one row = one designation's CBP
plan = one approval request). Fully self-contained — re-implements the send-for-approval + MDO
email logic directly against the DB.

**What it handles**: If the `role_mapping_id`, `recommendation_id`, or `mdo_id` column is entirely
missing from the input file, the script aborts immediately (these three columns are mandatory). A
row with a blank/invalid `role_mapping_id`/`recommendation_id`, or blank `mdo_id`, is skipped, not
fatal to the batch. A row whose CBP plan isn't found is skipped (`SKIPPED_NO_CBP_PLAN`).

⚠️ **Does not handle re-runs safely** — there is no dedup/already-submitted check. Every run creates
fresh approval requests for every eligible row. Re-running the same input file creates duplicate
approval requests and sends duplicate emails; only run this once per input file.

**Env required**: `DATABASE_URL`, `KB_BASE_URL`, `KB_AUTH_TOKEN`, `NOTIFICATION_BASE_URL`,
`MDO_PORTAL_URL`, `ENABLE_EMAIL_NOTIFICATION`

**Input**: `.xlsx`/`.xlsm` or `.csv`. If any of the 3 mandatory columns is entirely missing from
the file, the script aborts immediately (not just the affected row).

| Column | Required | Notes |
|---|:---:|---|
| `role_mapping_id` | ✅ | |
| `recommendation_id` | ✅ | |
| `mdo_id` | ✅ | |
| `state_center_id` | | optional, echoed into the outcome CSV only |
| `department_id` | | optional, echoed into the outcome CSV only |
| `org_type` | | optional, echoed into the outcome CSV only |
| `state_center_name` | | optional, echoed into the outcome CSV only |
| `department_name` | | optional, echoed into the outcome CSV only |
| `designation` | | optional, echoed into the outcome CSV only |

**Output**: `<dir of --excel>/batch_send_approval_requests_<timestamp>.csv` (input columns +
`approval_request_id`, `request_name`, `designation_count`, `approval_request_item_ids`, `status`,
`error`); log at `bulk_scripts/logs/batch_send_approval_requests_<timestamp>.log`.

**Things to know**
- A row marked "succeeded" in the output means the approval request was created — it does **not**
  guarantee the notification email reached anyone. Sending the email is best-effort: if it fails
  (approver not found, notification service down, etc.), only a warning is logged and the row still
  counts as a success.
- The approval request's display name is built as "AI CBP for `<designation>`" — it uses the iGOT
  designation name if one is set on the role mapping, and only falls back to the plain designation
  name when there's no iGOT name. Capped at 100 characters, with `...` appended if trimmed — two
  requests with very long, similar designation names could look identical in a review list.
- A row can be "skipped" for two normal, expected reasons that are not failures: no matching CBP
  plan was found, or the role mapping wasn't found for that user — both show up as skips in the
  report, not as errors, so don't assume "0 failures" means every row was submitted.

```bash
# 1. Dry run
python bulk_scripts/batch_send_approval_requests.py --excel <path/to/file.xlsx> --user-id <uuid>

# 2. Execute (only once per input file -- see the re-run warning above)
python bulk_scripts/batch_send_approval_requests.py --excel <path/to/file.xlsx> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/batch_send_approval_requests.py --excel <path/to/file.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## 6. `bulk_training_plan_approval.py`

**What it does**: Bulk-**publishes** Training Plan approval requests that already exist and are
`PENDING` — it does not create requests. Talks directly to the CB ext course service's AICBP
create/publish APIs and the shared DB.

**What it handles**: A blank/invalid `approval_request_id` is skipped, not fatal. A request that
exists but doesn't belong to `--user-id`, or has no items to derive a CBP plan name from, is
reported `FAILED` with a clear reason. A request already fully `APPROVED` is reported
`already_approved` (no writes). A request with some items still `PENDING` from a prior partial
failure (e.g. a transient iGOT error) picks up exactly where it left off on the next run — only an
item whose create+publish both succeed gets written; a failed item stays `PENDING` and is retried
automatically. The approver's user token is always fetched fresh from SSO at startup — there's no
token env var to configure or go stale.

**Env required**: `DATABASE_URL`, `CB_EXT_COURSE_SERVICE_URL`, `NOTIFICATION_BASE_URL`,
`ENABLE_EMAIL_NOTIFICATION`, `SUNBIRD_SSO_URL`, `SUNBIRD_SSO_REALM`, `TOKEN_CLIENT_ID`,
`TOKEN_CLIENT_SECRET`, `TOKEN_USERNAME`, `TOKEN_PASSWORD` (the last two are the approver's SSO
credentials — never commit real values).

⚠️ **`CB_EXT_COURSE_SERVICE_URL` and running from a jumphost**: this script calls the CB ext course
service's admin APIs directly to create and publish the plan. If you're running the script from a
jumphost (not from inside the cluster), that service isn't reachable directly — you must port-forward
it to the jumphost first:

```bash
kubectl port-forward -n dev pod/cb-ext-course-service-75b747db79-plj6m 17005:7005
```

Then point `CB_EXT_COURSE_SERVICE_URL` in `.env` at the forwarded local port, e.g.:

```
CB_EXT_COURSE_SERVICE_URL="http://localhost:17005"
```

The pod name and local port above are examples — use the actual pod name for the environment
you're targeting (`kubectl get pods -n dev | grep cb-ext-course-service`), and keep the port-forward
running in a separate terminal for the duration of the script run.

**Input**: CSV/Excel.

| Column | Required | Notes |
|---|:---:|---|
| `approval_request_id` | ✅ | blank/invalid value skips that row, not fatal |
| `due_date` | | overrides `--due-date` for that row only |
| *(anything else)* | | passed through unchanged into the outcome CSV |

**Output**: `<dir of --excel>/bulk_training_plan_approval_<timestamp>.csv` — every input column (in
original order) followed by `cbp_plan_name, cbp_plan_id, due_date, status, error, published_by`;
log at `bulk_scripts/logs/bulk_training_plan_approval_<timestamp>.log`.

**Things to know**
- `published_by` is the approver's user id, extracted from the fetched user token's own `sub` claim
  (not `--user-id`, which only scopes which requests can be looked up). It's written both to the
  outcome CSV and to `approval_requests.published_by` in the DB once a request is flipped to
  `APPROVED`.
- The **row status is strictly pass/fail** — there is no "partially published" status. If a request
  has 3 designations and 2 publish successfully but 1 fails, the row still shows `failed`, even
  though 2 designations did get published. Don't assume a `failed` row means nothing happened.
- A request that partially fails can stay in a pending, retryable state **indefinitely** — the
  system has no "permanently failed" status for a request, so it will keep being retried on every
  future run until every one of its designations eventually succeeds (or someone investigates why
  it keeps failing).
- The CBP plan's name is always taken from the **request's own record in the database**, never from
  anything typed in your spreadsheet — a `designation` column in your input file is for your own
  reference/logging only, it does not affect the published plan's name.
- The plan name is built as "AI CBP for `<designation>`" — it uses the iGOT designation name if one
  is set on the request's item, and only falls back to the plain designation name when there's no
  iGOT name.
- Plan names longer than 70 characters are silently cut short — two long, similar designation names
  could end up looking identical in the published plan list.
- Sending the "approved" notification email is best-effort, same as script 5 — a row can show
  `approved` even if the email never went out.
- This script re-authenticates from scratch on every run (fetches a fresh login token) — if the
  configured credentials are wrong or expired, the **entire run aborts immediately**, before any
  rows are processed.
- Safe to re-run: an already-fully-approved request is reported as such with no changes made, and a
  partially-completed request automatically resumes only the parts that are still outstanding.

```bash
# 1. Dry run
python bulk_scripts/bulk_training_plan_approval.py --excel <path/to/plans.xlsx> --user-id <uuid> --due-date 2027-03-31

# 2. Execute
python bulk_scripts/bulk_training_plan_approval.py --excel <path/to/plans.xlsx> --user-id <uuid> --due-date 2027-03-31 --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/bulk_training_plan_approval.py --excel <path/to/plans.xlsx> --user-id <uuid> --due-date 2027-03-31 --execute --batch-size 20
```

---

## 7. `bulk_update_courses_by_role_mapping.py`

**What it does**: Bulk-removes and/or bulk-adds courses on a `role_mappings` row's recommendation
and CBP plan, driven by an input file. Pure DB, no LLM/API calls — use it to hand-correct a course
list (e.g. swap out a mis-recommended course) without re-running generation or the app's UI.

**What it handles**: For every input row, updates **both**
`recommended_courses.filtered_courses` (the recommendation shown in the UI) and
`cbp_plans.selected_courses` (every CBP plan built on that role mapping) — matching courses to
remove by their `identifier` field, the same field the app's own single-course delete uses. A
course to add is resolved against `course_metadata_weightage` (the same table the recommendation
pipeline enriches from) so it carries the same keys as a generated course and is indistinguishable
downstream; an identifier not found there is **not** added and is reported under
`unresolved_courses` rather than inserted as a bare id that would render as an empty card. A
manually added course is stamped with `--relevancy` (default 90, the app's own default for
identifier-added courses) and `--rationale`, and `filtered_courses` is re-sorted by relevancy
descending after an add, matching how generation persists it.

Every row lands in exactly one of these outcomes, all non-fatal to the rest of the run (one bad row
never aborts the batch):

| Status | When |
|---|---|
| `role_mapping_not_found` | `role_mapping_id` doesn't exist in `role_mappings` |
| `user_mismatch` | role mapping exists but is owned by someone other than `--user-id` |
| `no_records` | role mapping exists but has no `recommended_courses` **and** no `cbp_plans` rows at all |
| `recommendation_in_progress` | the role mapping's recommendation is still `IN_PROGRESS` — skipped whole rather than half-edited under a running generation (the same guard the app's API returns a 409 for) |
| `additions_unresolved` | every requested addition failed to resolve against `course_metadata_weightage`, and there were no removals either |
| `no_change_needed` | every removal was already absent and every addition was already present — nothing left to do |
| `would_update` | dry-run only: at least one real add/remove would happen |
| `updated` | `--execute` only: the add/remove was persisted |
| `error` | row-level input problem — `missing_columns`, `ambiguous_columns`, `no_courses_specified` (both course columns empty), `identifier_in_both` (same id in both columns), or `invalid_role_mapping_id` |

Safe to re-run: an identifier already gone (or already present) is reported, not an error, so a row
with nothing left to do comes back as `no_change_needed` rather than failing. Every row runs in its
own transaction with `SELECT ... FOR UPDATE` under `--execute`, so a concurrent app edit can't be
silently clobbered by the read-modify-write.

**Env required**: `DATABASE_URL` only (pure DB script, no GCS/Gemini).

**Input**: `.csv` (tab- or comma-delimited, auto-detected) or `.xlsx`/`.xlsm`. Headers are matched
case/space/underscore-insensitively, so `Role Mapping ID`, `role_mapping_id` and `role-mapping-id`
are all the same column.

| Column | Required | Accepted header aliases | Notes |
|---|:---:|---|---|
| `role_mapping_id` | ✅ | `role mapping`, `role mapping ids` | must exist in `role_mappings`, else `role_mapping_not_found` |
| `courses_to_be_removed` | | `course_to_be_removed`, `courses to remove`, `course to remove`, `courses to be deleted`, `remove courses`, `removed courses`, `course ids to be removed` | identifier(s) to remove; comma/semicolon/pipe/newline/space-separated or a JSON list — at least one of this or the add column is required |
| `courses_to_be_added` | | `course_to_be_added`, `courses to add`, `course to add`, `add courses`, `added courses`, `new courses`, `course ids to be added` | identifier(s) to add; same list formats as removals |

A courses cell may hold a single identifier, a separated list, or a JSON/Python-style list — all
parse to the same result, with duplicates within a cell collapsed. An unquoted bracketed list split
across columns by a comma-delimited CSV (`[do_1,do_2]` written without surrounding quotes) is
stitched back together automatically; if a row still has more fields than the header after that
repair, it's rejected as `ambiguous_columns` rather than guessed at.

**Output**: log at `bulk_scripts/logs/<input-file-name>_<timestamp>.log`; outcome CSV written
**alongside the input file** at `<input-file-name>_<timestamp>.csv` — one row per input line,
columns: `row_no, mode, status, reason, role_mapping_id, role_mapping_user_id, state_center_id,
department_id, designation_name, requested_removals, requested_additions, recommendation_ids,
recommendation_statuses, rec_courses_before, rec_courses_after, removed_from_recommendations,
added_to_recommendations, cbp_plan_ids, cbp_courses_before, cbp_courses_after,
removed_from_cbp_plans, added_to_cbp_plans, added_course_names, not_found_courses,
unresolved_courses, already_present_courses`.

**Things to know**
- `--user-id` is **mandatory** and scopes every row: a role mapping owned by anyone else is
  skipped as `user_mismatch`, never touched.
- `recommended_courses.actual_courses` (the raw vector-search audit trail) is deliberately left
  untouched by both operations — only `filtered_courses` drives what the user sees.
  `suggested_courses` and `user_added_courses` are not touched either; an identifier that only
  exists there is reported under `not_found_courses` on removal, not removed.
- Re-generating recommendations afterwards discards every edit made here (removed courses come
  back, added ones disappear) — run this only after generation has settled for that role mapping.
- If the same `role_mapping_id` appears on several rows, each row is processed independently
  against the identifiers on that row (they are not merged).

```bash
# 1. Dry run
python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel <path/to/changes.csv> --user-id <uuid>

# 2. Execute
python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel <path/to/changes.csv> --user-id <uuid> --execute

# 3. Execute with a specific batch size (default 10)
python bulk_scripts/bulk_update_courses_by_role_mapping.py --excel <path/to/changes.xlsx> --user-id <uuid> --execute --batch-size 20
```

---

## Typical end-to-end order

If you're running these as a pipeline for a new state/department onboarding, the usual order is:

1. `batch_copy_all_documents.py` — get source documents onto the target user (if needed)
2. `batch_document_summary.py` — summarize those documents
3. `batch_rolemapping_generate.py` — generate role mappings per designation (needs step 2's summaries)
4. `batch_generate_and_save_cbp_plan.py` — generate course recommendations + CBP plans per role mapping
5. `batch_send_approval_requests.py` — submit CBP plans for approval (⚠️ not re-run-safe — see above)
6. `bulk_training_plan_approval.py` — publish the approved Training Plans

Each step's outcome CSV tells you what's ready to feed into the next step (e.g. which
`role_mapping_id`s succeeded in step 3 are the ones to hand to step 4).

`bulk_update_courses_by_role_mapping.py` (script 7) is not part of this sequence — it's an ad-hoc
utility for hand-correcting a course list after step 4 has already run, not a pipeline stage.

## Running scripts in the background (VM / jumphost)

Long-running scripts (document summarization, CBP plan generation, etc.) can take hours. On a VM
or jumphost, run them detached from your terminal so they keep going even if your SSH session
drops.

**Start the script in the background:**

```bash
nohup uv run bulk_scripts/batch_document_summary.py \
  --excel "/path/to/input.csv" --user-id <uuid> --execute \
  > bulk_scripts/logs/batch_document_summary_console_<label>.out 2>&1 &
disown
```

- `nohup ... &` — runs the command in the background and keeps it alive after you log out.
- `> ...console_<label>.out 2>&1` — redirects stdout+stderr to a file (in addition to the script's
  own timestamped log file under `bulk_scripts/logs/`, which is written either way). Name the
  `<label>` something identifying the run (e.g. the date or input file name) so you can find it
  later.
- `disown` — detaches the job from the current shell so closing the terminal can't send it a
  hangup signal. Must be run right after the `&` command, in the same shell session.

**Check whether it's still running:**

```bash
ps aux | grep batch_document_summary.py
```

If the only line returned is the `grep` command matching itself, the script has finished (or
crashed) — it is NOT running anymore. A still-running script shows a second line with the actual
`python`/`uv run` process.

**Watch its progress live:**

```bash
tail -f bulk_scripts/logs/batch_document_summary_console_<label>.out
```

Press `Ctrl+C` to stop watching — this does NOT stop the script itself, it only stops `tail`.

**How to tell it's actually done (not just idle/stuck):** the script's last few log lines report
the outcome CSV and log file paths (e.g. `report: ...` / `per-row status: ...` /
`Outcome CSV written to: ...`) — these are only printed once the run has fully finished. If the
log just stops mid-batch with no such closing lines, and `ps aux` still shows the process, it's
still running; if `ps aux` shows nothing and there are no closing lines, it likely crashed — check
the tail of the log/`.out` file for a traceback.

### Killing a background run

**1. Find the process ID (PID):**

```bash
ps aux | grep batch_document_summary.py
```

**2. Kill it gracefully first** (lets it close files cleanly):

```bash
kill <PID>
```

**3. If it's still running after a few seconds, force-kill it:**

```bash
kill -9 <PID>
```

**Or, one-liner to find and kill by name** (kills every process matching, so double-check nothing
else legitimate matches first):

```bash
pkill -f batch_document_summary.py
# or, if it won't stop:
pkill -9 -f batch_document_summary.py
```

Killing a script mid-run is safe: every script's outcome CSV and log file are flushed to disk
incrementally as each row/document finishes, so whatever's already been written stays valid — you
just won't get a final "RUN SUMMARY" for the rows that hadn't been reached yet. Re-running the
same command afterwards picks up safely where it left off, for every script except
`batch_send_approval_requests.py` (see its re-run warning above).

## If something goes wrong

- Read the outcome CSV's `status`/`error` columns first — every failure has a reason there.
- Check the log file for the same run (same timestamp suffix) for full tracebacks.
- Every script here is safe to re-run **except `batch_send_approval_requests.py`** (see its
  warning above) — for the rest, you can fix the underlying issue and run the same command again;
  already-done work will be skipped or resumed correctly.

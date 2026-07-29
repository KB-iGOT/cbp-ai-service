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
`GOOGLE_PROJECT_ID` (loaded via `src.core.configs.settings`, same as script 2).

**Input**: `.xlsx`/`.csv`; ALL of `state_center_id, department_id, org_type, state_center_name,
department_name, designation` are mandatory columns — missing any of them aborts the whole run.

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

**Input**: `.xlsx`/`.xlsm` or `.csv` with 7 columns — only `role_mapping_id` (UUID) is mandatory;
`state_center_id, department_id, org_type, state_center_name, department_name, designation` are
optional and only echoed into the outcome CSV.

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

**Input**: `.xlsx`/`.xlsm` or `.csv` with mandatory columns `role_mapping_id`, `recommendation_id`,
`mdo_id`; optional/echoed-only columns `state_center_id`, `department_id`, `org_type`,
`state_center_name`, `department_name`, `designation`.

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

**Input**: CSV/Excel with mandatory column `approval_request_id`; an optional per-row `due_date`
column overrides `--due-date` for that row; every other input column is passed through unchanged.

**Output**: `<dir of --excel>/bulk_training_plan_approval_<timestamp>.csv` — every input column (in
original order) followed by `cbp_plan_name, cbp_plan_id, due_date, status, error, published_by`;
log at `bulk_scripts/logs/bulk_training_plan_approval_<timestamp>.log`.

**Things to know**
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

## If something goes wrong

- Read the outcome CSV's `status`/`error` columns first — every failure has a reason there.
- Check the log file for the same run (same timestamp suffix) for full tracebacks.
- Every script here is safe to re-run **except `batch_send_approval_requests.py`** (see its
  warning above) — for the rest, you can fix the underlying issue and run the same command again;
  already-done work will be skipped or resumed correctly.

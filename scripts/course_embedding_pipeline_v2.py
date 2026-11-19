"""
Course embedding pipeline v2 (CSV input).

Reads course data from a CSV export, generates description / keywords /
combined embeddings via Gemini, and upserts them into the
`course_metadata_weightage_v2` table in Postgres (pgvector).

Differences from course_embedding_pipeline.py (which pulls from the KB API):
  * Input is a CSV file, not the KB content search API.
  * Target table is course_metadata_weightage_v2 — the same schema as
    course_metadata_weightage plus a `course_level` text column.
  * competencies_v6 is constructed from the CSV's m_area / m_theme /
    m_subtheme / m_domain_subtheme columns and holds only the three name
    fields (the CSV carries no KCM RefIds or identifiers).
  * `source` and `organisation` both come from content_provider_name.
  * course_level (from m_level) is included in the combined embedding text.
  * language, duration and version_key are not present in the CSV, so those
    columns are kept for schema compatibility but left NULL.

Expected CSV columns:
    identifier | content_id      Course identifier
    content_name                 Course name
    content_provider_name        Used for BOTH source and organisation
    description, instructions    Free text (HTML is flattened)
    keywords                     Comma-separated
    difficulty_level             Beginner / Intermediate / Advanced / Expert
    m_area                       Competency area: Behavioural / Functional / Domain
    m_theme, m_subtheme          Theme / sub-theme (empty for Domain rows)
    m_domain_subtheme            Domain value; used for BOTH theme and
                                 sub-theme when m_area is "Domain"
    m_level                      Course level: Operational / Tactical / Strategic

Rows whose m_area is "NA" are ingested with competencies_v6 = [] and a NULL
course_level.

Configuration is read from the environment; a .env file in the current
directory or the repo root is loaded automatically (real environment
variables take precedence over .env values).

Required environment variables:
    DATABASE_URL                    Postgres connection string
                                     e.g. postgresql://user:pass@host:5432/dbname
                                     (a "postgresql+asyncpg://" prefix is accepted)
    GOOGLE_API_KEY                  Gemini API key

Optional environment variables (CLI args take precedence):
    CSV_PATH                         Input CSV path
    BATCH_SIZE                       Courses processed+inserted per batch (default: 20)
    CONCURRENCY                      Max concurrent embedding tasks (default: 20)
    GOOGLE_EMBEDDING_MODEL           Gemini embedding model (default: gemini-embedding-2)
    EMBEDDING_OUTPUT_DIMENSIONALITY  Output embedding dimensionality (default: 1536)

Usage:
    python course_embedding_pipeline_v2.py --csv "/path/to/courses.csv" \
        [--batch-size N] [--concurrency N] [--limit N] [--dry-run]

Example:
    python scripts/course_embedding_pipeline_v2.py \
        --csv ~/Downloads/"updated_Matched with Excel.csv" \
        --batch-size 50 --concurrency 10
"""

import os
import csv
import sys
import json
import argparse
import asyncpg
from uuid import uuid4
from google import genai
from google.genai import types
import re
import asyncio
from bs4 import BeautifulSoup
from pathlib import Path
from typing import List, Dict, Any, Tuple, Iterator


def _load_dotenv():
    """Populate os.environ from a .env file (cwd first, then this file's repo root) so the settings
    below can be read without exporting them by hand. Real environment variables win (never
    overwritten). Uses python-dotenv if installed, otherwise a minimal hand-parser."""
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


_load_dotenv()  # so _require_env() below can read values populated from .env


TABLE_NAME = "course_metadata_weightage_v2"


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Generate course embeddings from a CSV and upsert them into Postgres."
    )
    parser.add_argument(
        "--csv", type=str, default=None,
        help="Path to the input CSV (overrides CSV_PATH env var)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="Courses processed and inserted per batch (overrides BATCH_SIZE env var; default: 20)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Max concurrent embedding tasks (overrides CONCURRENCY env var; default: 20)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N rows from the CSV (useful for a trial run)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse the CSV and print what would be ingested, without calling Gemini or writing to the DB"
    )
    return parser.parse_args()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _asyncpg_dsn(url: str) -> str:
    """asyncpg needs a plain postgresql:// DSN; .env carries the SQLAlchemy
    'postgresql+asyncpg://' dialect form used by the app."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


_args = _parse_args()

# --- Configuration ---
DATABASE_URL = _asyncpg_dsn(_require_env("DATABASE_URL"))

CSV_PATH = _args.csv or os.environ.get("CSV_PATH")
if not CSV_PATH:
    raise SystemExit("No input CSV: pass --csv /path/to/file.csv or set CSV_PATH.")

# The embedding model to use
EMBEDDING_MODEL = os.environ.get("GOOGLE_EMBEDDING_MODEL", "gemini-embedding-2")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_OUTPUT_DIMENSIONALITY", 1536))
BATCH_SIZE = _args.batch_size or int(os.environ.get("BATCH_SIZE", 20))
CONCURRENCY = _args.concurrency or int(os.environ.get("CONCURRENCY", 20))

# The client is only needed when embeddings are actually generated.
client = None if _args.dry_run else genai.Client(api_key=_require_env("GOOGLE_API_KEY"), vertexai=False)


# --- Helper Functions ---
def parse_html_to_text(raw_html):
    """Parse HTML content to plain text using BeautifulSoup."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    flat = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()
    return flat


def split_keywords(raw: str) -> List[str]:
    """Split the CSV's comma-separated keywords column into a list."""
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def build_competencies_v6(row: Dict[str, str]) -> List[Dict[str, str]]:
    """Build the competencies_v6 array from the CSV's m_* columns.

    For Domain rows the CSV leaves m_theme/m_subtheme empty and puts the value in
    m_domain_subtheme, which is used for BOTH the theme and the sub-theme name.
    Rows with no usable area (m_area == "NA") produce an empty list.
    """
    area = (row.get("m_area") or "").strip()
    if not area or area.upper() == "NA":
        return []

    if area.lower() == "domain":
        domain_value = (row.get("m_domain_subtheme") or "").strip()
        theme = sub_theme = domain_value
    else:
        theme = (row.get("m_theme") or "").strip()
        sub_theme = (row.get("m_subtheme") or "").strip()

    return [{
        "competencyAreaName": area,
        "competencyThemeName": theme,
        "competencySubThemeName": sub_theme,
    }]


def normalise_level(raw: str) -> str | None:
    """Course level from m_level; 'NA' and blanks become NULL."""
    value = (raw or "").strip()
    if not value or value.upper() == "NA":
        return None
    return value


def build_description_text(data: Dict[str, Any]) -> str:
    """Build text for description_embedding."""
    description = (data.get("description") or "").strip()
    return f"title: {data.get('name', '').strip()} | text: Description: {description}"


def build_keywords_text(data: Dict[str, Any]) -> str:
    """Build text for keywords_embedding."""
    keywords = data.get("keywords") or []
    kw_str = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
    return f"title: {data.get('name', '').strip()} | text: Keywords: {kw_str}"


def build_combined_text(data: Dict[str, Any]) -> str:
    """Build text for combined embedding:
    name + instructions + source + difficultyLevel + courseLevel + competencies_v6."""
    course_name = data.get("name", "").strip()

    instructions = ""
    if data.get("instructions"):
        instructions = parse_html_to_text(data["instructions"])

    formatted_comps = []
    if data.get("competencies_v6") and isinstance(data["competencies_v6"], list):
        for i, comp in enumerate(data["competencies_v6"], 1):
            area = comp.get("competencyAreaName", "None")
            theme = comp.get("competencyThemeName", "None")
            sub_theme = comp.get("competencySubThemeName", "None")
            formatted_comps.append(f"[{i}] Type: {area} -> Theme: {theme} -> Sub-Theme: {sub_theme}")
    comp_str = " | ".join(formatted_comps)

    source = (data.get("source") or "").strip()
    difficulty_level = (data.get("difficultyLevel") or "").strip()
    course_level = (data.get("courseLevel") or "").strip()

    text_body = (
        f"Name: {course_name} | Instructions: {instructions} | "
        f"Source: {source} | Difficulty Level: {difficulty_level} | "
        f"Course Level: {course_level} | Competencies: {comp_str}"
    )
    return f"title: {course_name} | text: {text_body}"


def read_csv_courses(path: str, limit: int | None = None) -> List[Dict[str, Any]]:
    """Read the CSV and normalise each row into the dict shape the builders expect."""
    csv.field_size_limit(10 ** 7)
    required = {"identifier", "content_name", "content_provider_name", "m_area", "m_level"}

    courses: List[Dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    missing_identifier = 0

    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = set(reader.fieldnames or [])
        absent = required - headers
        if absent:
            raise SystemExit(
                f"CSV is missing required column(s): {', '.join(sorted(absent))}\n"
                f"Found columns: {', '.join(sorted(headers))}"
            )

        for row in reader:
            identifier = (row.get("identifier") or row.get("content_id") or "").strip()
            if not identifier:
                missing_identifier += 1
                continue
            if identifier in seen:
                duplicates += 1
                continue
            seen.add(identifier)

            provider = (row.get("content_provider_name") or "").strip()

            courses.append({
                "identifier": identifier,
                "name": (row.get("content_name") or "").strip(),
                "description": (row.get("description") or "").strip() or None,
                "instructions": (row.get("instructions") or "").strip() or None,
                "keywords": split_keywords(row.get("keywords") or ""),
                "difficultyLevel": (row.get("difficulty_level") or "").strip() or None,
                # source and organisation are both the content provider
                "source": provider,
                "organisation": [provider] if provider else [],
                "competencies_v6": build_competencies_v6(row),
                "courseLevel": normalise_level(row.get("m_level")),
            })

            if limit is not None and len(courses) >= limit:
                break

    if missing_identifier:
        print(f"  Skipped {missing_identifier} row(s) with no identifier.")
    if duplicates:
        print(f"  Skipped {duplicates} duplicate identifier(s).")
    return courses


async def generate_embedding(text):
    """Generates an embedding for a single text asynchronously."""
    try:
        response = await client.aio.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=text,
            config=types.EmbedContentConfig(
                output_dimensionality=EMBEDDING_DIMENSION,
            )
        )
        if response and response.embeddings[0]:
            return response.embeddings[0], response.metadata
        else:
            print(f"Warning: No embedding generated for text: {text[:50]}...")
            return None, None
    except Exception as e:
        print(f"Error generating embedding for text: {text[:50]}... - {e}")
        return None, None


async def process_single_course(data: Dict[str, Any]) -> Dict[str, Any] | None:
    """Process a single course: generate three separate embeddings asynchronously."""
    identifier = data.get("identifier")
    competencies_v6 = json.dumps(data.get("competencies_v6") or [])

    description_text = build_description_text(data)
    keywords_text = build_keywords_text(data)
    combined_text = build_combined_text(data)

    # Generate all three embeddings concurrently
    (desc_emb, desc_meta), (kw_emb, _), (comb_emb, _) = await asyncio.gather(
        generate_embedding(description_text),
        generate_embedding(keywords_text),
        generate_embedding(combined_text),
    )

    if desc_emb is None or kw_emb is None or comb_emb is None:
        print(f"  Skipping {identifier} — one or more embeddings failed.")
        return None

    return {
        "course_id": str(uuid4()),
        "identifier": identifier,
        "name": data.get("name"),
        "keywords_pg": data.get("keywords") or [],
        "competencies_v6": competencies_v6,
        "instructions": data.get("instructions"),
        "description": data.get("description"),
        # language / duration / version_key are absent from the CSV
        "language_pg": [],
        "difficulty_level": data.get("difficultyLevel"),
        "duration": None,
        "organisation_pg": data.get("organisation") or [],
        "source": data.get("source") or None,
        "course_level": data.get("courseLevel"),
        "token_count": desc_meta.billable_character_count if desc_meta else None,
        "description_embedding": desc_emb.values,
        "keywords_embedding": kw_emb.values,
        "combined_embedding": comb_emb.values,
        "version_key": None,
    }


async def insert_batch_to_db(pool: asyncpg.Pool, records: List[Dict[str, Any]], existing: set) -> Tuple[int, int]:
    """Insert records into DB. Delete and re-insert if the identifier is already present.
    Returns (inserted, updated)."""
    inserted = 0
    updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for record in records:
                identifier = record["identifier"]
                is_update = identifier in existing
                if is_update:
                    await conn.execute(
                        f"DELETE FROM {TABLE_NAME} WHERE identifier = $1;",
                        identifier
                    )
                # description_tsv is a GENERATED ALWAYS column
                # (to_tsvector('english', COALESCE(description, ''))), so Postgres
                # computes it from `description`; naming it here is rejected.
                await conn.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (
                        id, identifier, name, keywords, competencies_v6,
                        instructions, description, language, difficulty_level,
                        duration, organisation, source, course_level, token_count,
                        description_embedding, keywords_embedding, combined_embedding,
                        version_key
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                              $15::vector, $16::vector, $17::vector, $18);
                    """,
                    record["course_id"], identifier, record["name"],
                    record["keywords_pg"], record["competencies_v6"],
                    record["instructions"], record["description"],
                    record["language_pg"], record["difficulty_level"],
                    record["duration"], record["organisation_pg"],
                    record["source"], record["course_level"],
                    record["token_count"],
                    str(record["description_embedding"]),
                    str(record["keywords_embedding"]),
                    str(record["combined_embedding"]),
                    record["version_key"]
                )
                if is_update:
                    updated += 1
                else:
                    inserted += 1
    return inserted, updated


async def fetch_existing_identifiers(pool: asyncpg.Pool) -> set:
    """Fetch all identifiers already present in the DB."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT identifier FROM {TABLE_NAME}")
        return {row["identifier"] for row in rows}


async def create_table_and_extension(pool: asyncpg.Pool):
    """Ensure pgvector extension and the v2 table exist.

    Schema mirrors course_metadata_weightage, plus `source` and `course_level`.
    """
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS public.{TABLE_NAME} (
                id uuid,
                identifier text COLLATE pg_catalog."default",
                name text COLLATE pg_catalog."default",
                keywords text[] COLLATE pg_catalog."default",
                competencies_v6 jsonb,
                instructions text COLLATE pg_catalog."default",
                description text COLLATE pg_catalog."default",
                language text[] COLLATE pg_catalog."default",
                difficulty_level text COLLATE pg_catalog."default",
                organisation text[] COLLATE pg_catalog."default",
                source text COLLATE pg_catalog."default",
                course_level text COLLATE pg_catalog."default",
                token_count double precision,
                description_embedding vector({EMBEDDING_DIMENSION}),
                keywords_embedding vector({EMBEDDING_DIMENSION}),
                combined_embedding vector({EMBEDDING_DIMENSION}),
                duration text COLLATE pg_catalog."default",
                version_key text COLLATE pg_catalog."default",
                description_tsv tsvector GENERATED ALWAYS AS (
                    to_tsvector('english'::regconfig, COALESCE(description, ''::text))
                ) STORED
            );
        """)
    print(f"Table public.{TABLE_NAME} and pgvector extension are ready.")


async def create_text_search_indexes(pool: asyncpg.Pool):
    """Create the GIN / btree / trgm indexes that mirror course_metadata_weightage."""
    async with pool.acquire() as conn:
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_keywords_gin_idx
            ON public.{TABLE_NAME} USING gin (keywords);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_organisation_gin_idx
            ON public.{TABLE_NAME} USING gin (organisation);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_language_gin_idx
            ON public.{TABLE_NAME} USING gin (language);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_competencies_gin_idx
            ON public.{TABLE_NAME} USING gin (competencies_v6);
        """)
        # No backfill needed: description_tsv is GENERATED ALWAYS from `description`,
        # so Postgres keeps it current and rejects any explicit write to it.
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_description_tsv_idx
            ON public.{TABLE_NAME} USING gin (description_tsv);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_identifier_idx
            ON public.{TABLE_NAME} USING btree (identifier);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_name_trgm_idx
            ON public.{TABLE_NAME} USING gin (name gin_trgm_ops);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_course_level_idx
            ON public.{TABLE_NAME} USING btree (course_level);
        """)
    print("Text search indexes (GIN on keywords/organisation/language/competencies_v6/description_tsv, "
          "btree on identifier and course_level, trgm on name) are ready.")


async def create_hnsw_indexes(pool: asyncpg.Pool):
    """Create HNSW cosine indexes on all three embedding columns."""
    async with pool.acquire() as conn:
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_desc_emb_hnsw_idx
            ON public.{TABLE_NAME} USING hnsw (description_embedding vector_cosine_ops);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_kw_emb_hnsw_idx
            ON public.{TABLE_NAME} USING hnsw (keywords_embedding vector_cosine_ops);
        """)
        await conn.execute(f"""
            CREATE INDEX IF NOT EXISTS {TABLE_NAME}_comb_emb_hnsw_idx
            ON public.{TABLE_NAME} USING hnsw (combined_embedding vector_cosine_ops);
        """)
    print("HNSW cosine indexes on description_embedding, keywords_embedding, combined_embedding are ready.")


def chunked(values: List[Any], size: int) -> Iterator[List[Any]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def run_dry_run(courses: List[Dict[str, Any]]):
    """Print what would be ingested, without calling Gemini or touching the DB."""
    from collections import Counter

    areas = Counter(
        (c["competencies_v6"][0]["competencyAreaName"] if c["competencies_v6"] else "(none)")
        for c in courses
    )
    levels = Counter(c["courseLevel"] or "(none)" for c in courses)

    print(f"\nParsed {len(courses)} course(s) from {CSV_PATH}")
    print(f"  competency areas: {dict(areas)}")
    print(f"  course levels:    {dict(levels)}")
    print(f"  no competencies:  {sum(1 for c in courses if not c['competencies_v6'])}")
    print(f"  no keywords:      {sum(1 for c in courses if not c['keywords'])}")
    print(f"  no description:   {sum(1 for c in courses if not c['description'])}")

    for label, want_domain in (("non-Domain", False), ("Domain", True)):
        sample = next(
            (c for c in courses
             if c["competencies_v6"]
             and (c["competencies_v6"][0]["competencyAreaName"].lower() == "domain") == want_domain),
            None,
        )
        if not sample:
            continue
        print(f"\n--- sample {label} course: {sample['identifier']} ---")
        print(f"  name:          {sample['name'][:80]}")
        print(f"  source:        {sample['source']}")
        print(f"  organisation:  {sample['organisation']}")
        print(f"  course_level:  {sample['courseLevel']}")
        print(f"  competencies_v6: {json.dumps(sample['competencies_v6'], ensure_ascii=False)}")
        print(f"  combined text: {build_combined_text(sample)[:300]}")


async def main():
    """Read the CSV, process courses concurrently in batches, insert into DB."""
    print(f"Reading courses from {CSV_PATH} ...")
    try:
        courses = read_csv_courses(CSV_PATH, limit=_args.limit)
    except FileNotFoundError:
        raise SystemExit(f"CSV not found: {CSV_PATH}")
    print(f"Read {len(courses)} course(s) from CSV.")

    if not courses:
        print("Nothing to do.")
        return

    if _args.dry_run:
        run_dry_run(courses)
        print("\nDry run — no embeddings generated, nothing written.")
        return

    pool = None
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        print("Database connection pool established successfully.")

        await create_table_and_extension(pool)

        existing = await fetch_existing_identifiers(pool)
        print(f"Found {len(existing)} already-processed course(s) in {TABLE_NAME}.")

        total_inserted = 0
        total_updated = 0
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def process_with_semaphore(course):
            async with semaphore:
                return await process_single_course(course)

        total_batches = (len(courses) + BATCH_SIZE - 1) // BATCH_SIZE
        for batch_no, batch in enumerate(chunked(courses, BATCH_SIZE), 1):
            print(f"\n===== Batch {batch_no}/{total_batches} ({len(batch)} courses) =====")

            tasks = [process_with_semaphore(course) for course in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            records = []
            for r in results:
                if isinstance(r, Exception):
                    print(f"  Course processing error: {type(r).__name__}: {r}")
                elif r is not None:
                    records.append(r)
            print(f"Embeddings generated: {len(records)}/{len(batch)}")

            if records:
                ins, upd = await insert_batch_to_db(pool, records, existing)
                total_inserted += ins
                total_updated += upd
                for r in records:
                    existing.add(r["identifier"])
                print(f"Batch done: {ins} inserted, {upd} updated.")

        print(f"\n===== Done. Inserted: {total_inserted} | Updated: {total_updated} =====")

        print("\nCreating text search indexes (GIN)...")
        await create_text_search_indexes(pool)

        print("\nCreating HNSW indexes...")
        await create_hnsw_indexes(pool)

    except Exception as e:
        print(f"Fatal error: {type(e).__name__}: {e}")
        raise
    finally:
        if pool:
            await pool.close()
            print("Database connection pool closed.")


if __name__ == "__main__":
    asyncio.run(main())

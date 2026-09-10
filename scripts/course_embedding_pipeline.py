"""
Course embedding pipeline (weightage variant).

Fetches course data from the KB content search API, generates description /
keywords / combined embeddings via Gemini, and upserts them into the
`course_metadata_weightage` table in Postgres (pgvector).

Usage:
    python course_embedding_pipeline.py [--batch-size N] [--concurrency N]

Configuration is read from the environment; a .env file in the current directory
or the repo root is loaded automatically (real environment variables take
precedence over .env values).

Required environment variables:
    DATABASE_URL                    Postgres connection string
                                     e.g. postgresql://user:pass@host:5432/dbname
                                     (a "postgresql+asyncpg://" prefix is accepted)
    KB_BASE_URL                     KB portal base URL
                                     e.g. https://portal.igotkarmayogi.gov.in
    KB_AUTH_TOKEN                   KB API bearer token (include the "Bearer " prefix)
    GOOGLE_API_KEY                  Gemini API key

Optional environment variables (can be overridden by CLI args for batch size/concurrency):
    BATCH_SIZE                       Courses fetched per API page (default: 20)
    CONCURRENCY                      Max concurrent embedding tasks (default: 20)
    GOOGLE_EMBEDDING_MODEL           Gemini embedding model (default: gemini-embedding-2)
    EMBEDDING_OUTPUT_DIMENSIONALITY  Output embedding dimensionality (default: 1536)

Example:
    # values come from .env, or export them explicitly:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    export KB_BASE_URL="https://portal.igotkarmayogi.gov.in"
    export KB_AUTH_TOKEN="Bearer eyJhbGciOi..."
    export GOOGLE_API_KEY="AQ...."
    python course_embedding_pipeline.py --batch-size 50 --concurrency 10
"""

import os
import json
import argparse
import asyncpg
from uuid import uuid4
from google import genai
from google.genai import types
import re
import asyncio
import httpx
from bs4 import BeautifulSoup
from pathlib import Path
from typing import List, Dict, Any, Tuple


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


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Generate course embeddings and upsert them into Postgres."
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="Courses fetched per API page (overrides BATCH_SIZE env var; default: 20)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Max concurrent embedding tasks (overrides CONCURRENCY env var; default: 20)"
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

# API Configuration
KB_BASE_URL = _require_env("KB_BASE_URL")
KB_AUTH_TOKEN = _require_env("KB_AUTH_TOKEN")

client = genai.Client(
    api_key=_require_env("GOOGLE_API_KEY")
)

# The embedding model to use
EMBEDDING_MODEL = os.environ.get("GOOGLE_EMBEDDING_MODEL", "gemini-embedding-2")
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_OUTPUT_DIMENSIONALITY", 1536))
BATCH_SIZE = _args.batch_size or int(os.environ.get("BATCH_SIZE", 20))  # Courses per batch
CONCURRENCY = _args.concurrency or int(os.environ.get("CONCURRENCY", 20))  # Max concurrent embedding tasks


# --- Helper Functions ---
def parse_html_to_text(raw_html):
    """Parse HTML content to plain text using BeautifulSoup."""
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "html.parser")
    flat = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()
    return flat


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
    """Build text for combined embedding: name + instructions + competencies_v6 + source + difficultyLevel."""
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

    text_body = (
        f"Name: {course_name} | Instructions: {instructions} | "
        f"Source: {source} | Difficulty Level: {difficulty_level} | Competencies: {comp_str}"
    )
    return f"title: {course_name} | text: {text_body}"


async def search_courses(offset: int = 0, limit: int = BATCH_SIZE, retries: int = 3) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch course data from the KB API with pagination and retry on failure."""
    payload = {
        "request": {
            "filters": {
                "primaryCategory": ["Course"],
                "status": ["Live"],
                "courseCategory": ["Course"]
            },
            "fields": [
                "name", "identifier", "description", "keywords",
                "organisation", "competencies_v6", "language", "duration",
                "instructions", "difficultyLevel", "source", "purpose", "versionKey"
            ],
            "sortBy": {"createdOn": "Desc"},
            "offset": offset,
            "limit": limit
        }
    }

    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                response = await http_client.post(
                    f"{KB_BASE_URL}/api/content/v1/search",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"{KB_AUTH_TOKEN}"
                    }
                )
                response.raise_for_status()
                data = response.json()
                result = data.get("result", {})
                content = result.get("content", [])
                count = result.get("count", 0)
                return content, count
        except Exception as e:
            print(f"  API fetch attempt {attempt}/{retries} failed at offset {offset}: {type(e).__name__}: {e}")
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
            else:
                raise


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
    name = data.get("name", "").strip()
    keywords = data.get("keywords")
    competencies_v6 = json.dumps(data.get("competencies_v6")) if data.get("competencies_v6") else None
    instructions = data.get("instructions")
    description = data.get("description")
    language = data.get("language")
    difficulty_level = data.get("difficultyLevel")
    duration = data.get("duration")
    organisation = data.get("organisation")
    version_key = str(data.get("versionKey", "")) if data.get("versionKey") else None

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

    keywords_pg = [k for k in keywords] if keywords else []
    language_pg = [l for l in language] if language and isinstance(language, list) else []
    organisation_pg = [o for o in organisation] if organisation and isinstance(organisation, list) else []

    return {
        "course_id": str(uuid4()),
        "identifier": identifier,
        "name": name,
        "keywords_pg": keywords_pg,
        "competencies_v6": competencies_v6,
        "instructions": instructions,
        "description": description,
        "language_pg": language_pg,
        "difficulty_level": difficulty_level,
        "duration": duration,
        "organisation_pg": organisation_pg,
        "token_count": desc_meta.billable_character_count if desc_meta else None,
        "description_embedding": desc_emb.values,
        "keywords_embedding": kw_emb.values,
        "combined_embedding": comb_emb.values,
        "version_key": version_key,
    }


async def insert_batch_to_db(pool: asyncpg.Pool, records: List[Dict[str, Any]], existing: Dict[str, str]) -> Tuple[int, int]:
    """Insert records into DB. Delete and re-insert if versionKey has changed. Returns (inserted, updated)."""
    inserted = 0
    updated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for record in records:
                identifier = record["identifier"]
                is_update = identifier in existing
                if is_update:
                    await conn.execute(
                        "DELETE FROM course_metadata_weightage WHERE identifier = $1;",
                        identifier
                    )
                await conn.execute(
                    """
                    -- description_tsv is a GENERATED ALWAYS column
                    -- (to_tsvector('english', COALESCE(description, ''))), so Postgres
                    -- computes it from `description`; naming it here is rejected.
                    INSERT INTO course_metadata_weightage (
                        id, identifier, name, keywords, competencies_v6,
                        instructions, description, language, difficulty_level,
                        duration, organisation, token_count,
                        description_embedding, keywords_embedding, combined_embedding,
                        version_key
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12,
                              $13::vector, $14::vector, $15::vector, $16);
                    """,
                    record["course_id"], identifier, record["name"],
                    record["keywords_pg"], record["competencies_v6"],
                    record["instructions"], record["description"],
                    record["language_pg"], record["difficulty_level"],
                    record["duration"], record["organisation_pg"],
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


async def fetch_existing_identifiers(pool: asyncpg.Pool) -> Dict[str, str]:
    """Fetch all identifiers and their versionKey already present in the DB."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT identifier, version_key FROM course_metadata_weightage")
        return {row["identifier"]: row["version_key"] for row in rows}


async def create_table_and_extension(pool: asyncpg.Pool):
    """Ensure pgvector extension and course_metadata_weightage table exist."""
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS public.course_metadata_weightage (
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
    print("Table course_metadata_weightage is ready.")


async def create_text_search_indexes(pool: asyncpg.Pool):
    """Create GIN/btree/trgm indexes for text-filterable and keyword-search fields."""
    async with pool.acquire() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_keywords_gin_idx
            ON public.course_metadata_weightage
            USING gin (keywords);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_organisation_gin_idx
            ON public.course_metadata_weightage
            USING gin (organisation);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_language_gin_idx
            ON public.course_metadata_weightage
            USING gin (language);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_competencies_gin_idx
            ON public.course_metadata_weightage
            USING gin (competencies_v6);
        """)
        # No backfill needed: description_tsv is GENERATED ALWAYS from `description`,
        # so Postgres keeps it current and rejects any explicit write to it.
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_description_tsv_idx
            ON public.course_metadata_weightage
            USING gin (description_tsv);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_identifier_idx
            ON public.course_metadata_weightage
            USING btree (identifier);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_name_trgm_idx
            ON public.course_metadata_weightage
            USING gin (name gin_trgm_ops);
        """)
    print("Text search indexes (GIN on keywords/organisation/language/competencies_v6/description_tsv, "
          "btree on identifier, trgm on name) are ready.")


async def create_hnsw_indexes(pool: asyncpg.Pool):
    """Create HNSW cosine indexes on all three embedding columns."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_desc_emb_hnsw_idx
            ON public.course_metadata_weightage
            USING hnsw (description_embedding vector_cosine_ops);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_kw_emb_hnsw_idx
            ON public.course_metadata_weightage
            USING hnsw (keywords_embedding vector_cosine_ops);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS course_metadata_weightage_comb_emb_hnsw_idx
            ON public.course_metadata_weightage
            USING hnsw (combined_embedding vector_cosine_ops);
        """)
    print("HNSW cosine indexes on description_embedding, keywords_embedding, combined_embedding are ready.")


async def main():
    """Fetch courses in paginated batches, process concurrently, insert into DB."""
    pool = None
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        print("Database connection pool established successfully.")

        await create_table_and_extension(pool)

        existing = await fetch_existing_identifiers(pool)
        print(f"Found {len(existing)} already-processed courses in DB.")

        offset = 0
        total_inserted = 0
        total_updated = 0
        total_skipped = 0
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def process_with_semaphore(course):
            async with semaphore:
                return await process_single_course(course)

        while True:
            print(f"\n===== Fetching batch at offset {offset} (limit {BATCH_SIZE}) =====")
            course_data, total_count = await search_courses(offset=offset, limit=BATCH_SIZE)

            if not course_data:
                print("No more courses to process.")
                break

            print(f"Fetched {len(course_data)} courses (total available: {total_count}).")

            courses_to_process = []
            for c in course_data:
                ident = c.get("identifier")
                api_version = str(c.get("versionKey", "")) if c.get("versionKey") else None
                if ident not in existing:
                    courses_to_process.append(c)
                elif existing[ident] != api_version:
                    courses_to_process.append(c)
                else:
                    total_skipped += 1

            skipped_this_batch = len(course_data) - len(courses_to_process)
            if skipped_this_batch:
                print(f"Skipping {skipped_this_batch} up-to-date course(s).")

            if courses_to_process:
                tasks = [process_with_semaphore(course) for course in courses_to_process]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                records = []
                for r in results:
                    if isinstance(r, Exception):
                        print(f"  Course processing error: {type(r).__name__}: {r}")
                    elif r is not None:
                        records.append(r)
                print(f"Embeddings generated: {len(records)}/{len(courses_to_process)}")

                if records:
                    ins, upd = await insert_batch_to_db(pool, records, existing)
                    total_inserted += ins
                    total_updated += upd
                    for r in records:
                        existing[r["identifier"]] = r["version_key"]
                    print(f"Batch done: {ins} inserted, {upd} updated.")

            offset += BATCH_SIZE
            if offset >= total_count:
                break

        print(f"\n===== Done. Inserted: {total_inserted} | Updated: {total_updated} | Skipped (up-to-date): {total_skipped} =====")

        print("\nCreating text search indexes (GIN)...")
        await create_text_search_indexes(pool)

        print("Creating HNSW indexes on embedding columns...")
        await create_hnsw_indexes(pool)

    except Exception as e:
        import traceback
        print(f"Fatal error: {type(e).__name__}: {e}")
        traceback.print_exc()
        raise SystemExit(1)
    finally:
        if pool:
            await pool.close()
            print("Database connection pool closed.")


if __name__ == "__main__":
    asyncio.run(main())
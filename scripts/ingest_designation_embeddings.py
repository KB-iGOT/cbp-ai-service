"""
Ingest iGOT designations from CSV into PostgreSQL with gemini-embedding-2 embeddings.

Flow per batch:
  1. Count tokens for the whole batch in one call.
  2. If total tokens <= MODEL_TOKEN_LIMIT → embed entire batch in one API call.
     Else → split into sub-batches that fit and embed each sub-batch.
  3. Run up to CONCURRENT_REQUESTS embed calls in parallel (asyncio.Semaphore).
  4. Upsert all records into designation_embeddings.

Usage:
    python scripts/ingest_designation_embeddings.py <csv_file> [--batch-size N] [--concurrency N]

Required environment variables:
    DATABASE_URL     Postgres connection string
                      e.g. postgresql://user:pass@host:5432/dbname
    GEMINI_API_KEY    Gemini API key

Optional environment variables (can be overridden by CLI args for batch size/concurrency):
    BATCH_SIZE              Designations embedded/upserted per batch (default: 100)
    CONCURRENT_REQUESTS     Max concurrent embed calls (default: 10)
    GOOGLE_EMBEDDING_MODEL  Gemini embedding model (default: gemini-embedding-2)

Example:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    export GEMINI_API_KEY="AQ...."
    python scripts/ingest_designation_embeddings.py /path/to/igot_designations.csv --batch-size 50 --concurrency 5
"""

import argparse
import asyncio
import csv
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import asyncpg
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Ingest iGOT designations from CSV into PostgreSQL with embeddings."
    )
    parser.add_argument(
        "csv_file", type=str,
        help="Path to the designations CSV file"
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="Designations embedded/upserted per batch (overrides BATCH_SIZE env var; default: 100)"
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Max concurrent embed calls (overrides CONCURRENT_REQUESTS env var; default: 10)"
    )
    return parser.parse_args()


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


_args = _parse_args()

DATABASE_URL = _require_env("DATABASE_URL")
CSV_FILE = _args.csv_file
EMBEDDING_MODEL     = os.environ.get("GOOGLE_EMBEDDING_MODEL", "gemini-embedding-2")
MODEL_TOKEN_LIMIT   = 8192
BATCH_SIZE          = _args.batch_size or int(os.environ.get("BATCH_SIZE", 100))
CONCURRENT_REQUESTS = _args.concurrency or int(os.environ.get("CONCURRENT_REQUESTS", 10))

gemini_client = genai.Client(
    api_key=_require_env("GEMINI_API_KEY")
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS designation_embeddings (
    id          TEXT        PRIMARY KEY,
    designation TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    embedding   vector(768) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_designation_embedding
    ON designation_embeddings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_designation_name_lower
    ON designation_embeddings (LOWER(designation));
"""

UPSERT_SQL = """
INSERT INTO designation_embeddings (id, designation, content, embedding)
VALUES ($1, $2, $3, $4::vector)
ON CONFLICT (id) DO UPDATE
    SET designation = EXCLUDED.designation,
        content     = EXCLUDED.content,
        embedding   = EXCLUDED.embedding,
        updated_at  = NOW();
"""


def load_csv(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_batches(rows: List[Dict[str, str]], size: int) -> List[List[Dict[str, str]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def format_for_matching(text: str) -> str:
    return f"task: sentence similarity | query: {text}"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def count_tokens(texts: List[str]) -> int:
    """Count total tokens for a list of texts in one async API call. Retries up to 2 times."""
    contents = [format_for_matching(t) for t in texts]
    response = await gemini_client.aio.models.count_tokens(
        model=EMBEDDING_MODEL,
        contents=contents,
    )
    return response.total_tokens


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def embed_texts(texts: List[str]) -> List[list]:
    """Embed a list of texts in one async API call. Retries up to 2 times."""
    contents = [format_for_matching(t) for t in texts]
    response = await gemini_client.aio.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=contents,
        config=types.EmbedContentConfig(output_dimensionality=768),
    )
    return [e.values for e in response.embeddings]


async def split_into_token_safe_subbatches(texts: List[str]) -> List[List[str]]:
    """
    Split texts into sub-batches where each fits within MODEL_TOKEN_LIMIT.
    Uses binary search to find the largest safe slice per iteration.
    """
    subbatches = []
    i = 0
    while i < len(texts):
        j = len(texts)
        while j > i:
            total = await count_tokens(texts[i:j])
            if total <= MODEL_TOKEN_LIMIT:
                break
            j = i + max(1, (j - i) // 2)
        subbatches.append(texts[i:j])
        i = j
    return subbatches


async def get_embeddings_for_batch(
    semaphore: asyncio.Semaphore,
    texts: List[str],
    batch_label: str,
) -> List[list]:
    """
    1. Count tokens for the full text list.
    2. If within limit → single embed call.
       Else → split into token-safe sub-batches and embed each.
    Runs concurrently up to CONCURRENT_REQUESTS at a time.
    """
    async with semaphore:
        total_tokens = await count_tokens(texts)
        print(f"    {batch_label}: {len(texts)} designations, {total_tokens} tokens", end="")

        if total_tokens <= MODEL_TOKEN_LIMIT:
            print(" → single embed call")
            return await embed_texts(texts)

        print(f" → exceeds {MODEL_TOKEN_LIMIT}, splitting into sub-batches")
        subbatches = await split_into_token_safe_subbatches(texts)
        print(f"    {batch_label}: {len(subbatches)} sub-batches")
        embeddings = []
        for sb_idx, sub in enumerate(subbatches, 1):
            sub_embs = await embed_texts(sub)
            embeddings.extend(sub_embs)
            print(f"    {batch_label} sub-batch {sb_idx}/{len(subbatches)}: {len(sub)} rows done")
        return embeddings


async def ensure_vector_extension(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")


async def ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(CREATE_TABLE_SQL)


async def ingest_batch(
    pool: asyncpg.Pool,
    semaphore: asyncio.Semaphore,
    batch: List[Dict[str, str]],
    batch_num: int,
    total_batches: int,
) -> int:
    batch_label = f"Batch {batch_num}/{total_batches}"
    texts = [row.get("designation", "") for row in batch]

    embeddings = await get_embeddings_for_batch(semaphore, texts, batch_label)

    records: List[Tuple[Any, ...]] = []
    for row, emb in zip(batch, embeddings):
        igot_id     = row.get("id", "")
        designation = row.get("designation", "")
        content     = format_for_matching(designation)
        records.append((igot_id, designation, content, str(emb)))

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT_SQL, records)

    print(f"    {batch_label}: upserted {len(records)} rows")
    return len(records)


async def main() -> None:
    csv_path = Path(CSV_FILE)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path.resolve()}")

    print(f"Loading CSV: {csv_path.resolve()}")
    rows = load_csv(csv_path)
    print(f"Total rows: {len(rows)}")
    print(f"Embedding model:      {EMBEDDING_MODEL}")
    print(f"Token limit:          {MODEL_TOKEN_LIMIT}")
    print(f"Batch size:           {BATCH_SIZE}")
    print(f"Concurrent requests:  {CONCURRENT_REQUESTS}")

    print("Connecting to database …")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)

    async with pool.acquire() as conn:
        await ensure_vector_extension(conn)
        await ensure_table(conn)
    print("Table ready.\n")

    batches = build_batches(rows, BATCH_SIZE)
    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    tasks = [
        ingest_batch(pool, semaphore, batch, i, len(batches))
        for i, batch in enumerate(batches, start=1)
    ]

    results = await asyncio.gather(*tasks)
    total_inserted = sum(results)

    await pool.close()
    print(f"\nDone. Total rows upserted: {total_inserted}")


if __name__ == "__main__":
    asyncio.run(main())

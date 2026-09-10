"""
Delete retired courses pipeline.

Fetches all course identifiers currently stored in `course_metadata_weightage`,
checks the KB content search API for which of those are now "Retired", and
deletes the matching rows from the table.

Usage:
    python delete_retired_courses_pipeline.py [--identifier-batch-size N]

Configuration is read from the environment; a .env file in the current directory
or the repo root is loaded automatically (real environment variables take
precedence over .env values).

Required environment variables:
    DATABASE_URL              Postgres connection string
                               e.g. postgresql://user:pass@host:5432/dbname
                               (a "postgresql+asyncpg://" prefix is accepted)
    KB_BASE_URL                KB portal base URL
                               e.g. https://portal.igotkarmayogi.gov.in
    KB_AUTH_TOKEN              KB API bearer token (include the "Bearer " prefix)

Optional environment variables (can be overridden by --identifier-batch-size):
    IDENTIFIER_BATCH_SIZE      Identifiers sent per API search call (default: 100)

Example:
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    export KB_BASE_URL="https://portal.igotkarmayogi.gov.in"
    export KB_AUTH_TOKEN="Bearer eyJhbGciOi..."
    python delete_retired_courses_pipeline.py --identifier-batch-size 200
"""

import argparse
import asyncio
import os
from pathlib import Path
from typing import List, Set

import asyncpg
import httpx


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
        description="Delete retired courses from course_metadata_weightage."
    )
    parser.add_argument(
        "--identifier-batch-size", type=int, default=None,
        help="Identifiers sent per API search call (overrides IDENTIFIER_BATCH_SIZE env var; default: 100)"
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

IDENTIFIER_BATCH_SIZE = _args.identifier_batch_size or int(os.environ.get("IDENTIFIER_BATCH_SIZE", 100))


def chunked(values: List[str], size: int) -> List[List[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


async def fetch_all_db_identifiers(pool: asyncpg.Pool) -> List[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT identifier
            FROM course_metadata_weightage
            WHERE identifier IS NOT NULL
              AND btrim(identifier) <> ''
            """
        )
    return [row["identifier"] for row in rows]


async def search_retired_batch(
    http_client: httpx.AsyncClient,
    identifier_batch: List[str],
    batch_num: int,
) -> Set[str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": KB_AUTH_TOKEN,
    }
    payload = {
        "request": {
            "filters": {
                "identifier": identifier_batch,
                "courseCategory": ["Course"],
                "primaryCategory": ["Course"],
                "status": ["Retired"],
            },
            "fields": ["identifier", "status"],
            "query": "",
            "limit": len(identifier_batch),
            "offset": 0,
            "sort_by": {},
        }
    }

    response = await http_client.post(
        f"{KB_BASE_URL}/api/content/v1/search",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()

    data = response.json()
    content = data.get("result", {}).get("content") or []
    retired = {item["identifier"] for item in content if item.get("identifier")}
    print(f"Batch {batch_num}: sent {len(identifier_batch)} identifiers, retired found: {len(retired)}")
    return retired


async def fetch_retired_identifiers_from_api(
    http_client: httpx.AsyncClient,
    db_identifiers: List[str],
) -> Set[str]:
    batches = chunked(db_identifiers, IDENTIFIER_BATCH_SIZE)
    retired: Set[str] = set()
    for i, batch in enumerate(batches, start=1):
        retired_batch = await search_retired_batch(http_client, batch, i)
        retired.update(retired_batch)
    return retired


async def delete_identifiers(conn: asyncpg.Connection, identifiers: List[str]) -> int:
    if not identifiers:
        return 0

    result = await conn.execute(
        "DELETE FROM course_metadata_weightage WHERE identifier = ANY($1::text[])",
        identifiers,
    )
    # result format: "DELETE <count>"
    return int(result.split()[-1])


async def main() -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    try:
        async with httpx.AsyncClient(timeout=120.0) as http_client:
            print("Step 1: Fetching all identifiers from DB...")
            db_identifiers = await fetch_all_db_identifiers(pool)
            print(f"Total identifiers in DB: {len(db_identifiers)}")

            print("\nStep 2: Fetching all retired identifiers from content search API...")
            retired_identifiers = await fetch_retired_identifiers_from_api(http_client, db_identifiers)
            print(f"Total retired identifiers from API: {len(retired_identifiers)}")

            to_delete = list(set(db_identifiers) & retired_identifiers)
            print(f"\nStep 3: Identifiers to delete (retired & in DB): {len(to_delete)}")
            print(f"Identifiers to delete: {to_delete}")

            if to_delete:
                async with pool.acquire() as conn:
                    async with conn.transaction():
                        deleted = await delete_identifiers(conn, to_delete)
                print(f"Deleted {deleted} rows from course_metadata_weightage.")
            else:
                print("Nothing to delete.")

    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
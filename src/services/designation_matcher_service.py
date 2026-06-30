from typing import Any, Dict, List

from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from ..core.configs import settings
from ..core.logger import logger
from .redis_service import redis_service


_genai_client: genai.Client | None = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _genai_client


def _cache_key(designation: str) -> str:
    return f"{settings.REDIS_DESIG_EMB_PREFIX}:{designation.lower()}"


def _format_for_matching(designation: str) -> str:
    return f"task: sentence similarity | query: {designation}"


async def _get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Return embeddings for texts, serving from Redis cache where available.
    Only calls Gemini API for cache misses.
    """
    keys = [_cache_key(t) for t in texts]
    cached_values = await redis_service.mget_json(keys)

    results: List[List[float] | None] = [None] * len(texts)
    uncached_indices: List[int] = []

    for i, cached in enumerate(cached_values):
        if cached is not None:
            results[i] = cached
        else:
            uncached_indices.append(i)

    if uncached_indices:
        uncached_texts = [texts[i] for i in uncached_indices]
        logger.info(f"Embedding cache miss: {len(uncached_texts)}/{len(texts)} — calling Gemini API")

        client = _get_genai_client()
        fresh_embeddings: List[List[float]] = []
        for i in range(0, len(uncached_texts), settings.DESIGNATION_EMBED_BATCH_SIZE):
            batch = uncached_texts[i: i + settings.DESIGNATION_EMBED_BATCH_SIZE]
            contents = [_format_for_matching(t) for t in batch]
            response = await client.aio.models.embed_content(
                model=settings.GOOGLE_EMBEDDING_MODEL,
                contents=contents,
                config=types.EmbedContentConfig(output_dimensionality=768),
            )
            fresh_embeddings.extend(e.values for e in response.embeddings)

        to_cache = {}
        for idx, emb in zip(uncached_indices, fresh_embeddings):
            results[idx] = emb
            to_cache[keys[idx]] = emb
        await redis_service.mset_json(to_cache)
    else:
        logger.info(f"All {len(texts)} designation embeddings served from Redis cache")

    return results  # type: ignore[return-value]


class DesignationMatcherService:
    """
    Matches user-provided designation names against the designation_embeddings table.

    Two strategies:
      1. Exact   — case-insensitive match using LOWER index (single query).
      2. Semantic — gemini-embedding-2 + pgvector cosine similarity fallback.
    """

    async def match_exact(
        self, db: AsyncSession, designation_names: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Case-insensitive exact match against designation_embeddings.
        Single DB query using LOWER(designation) = ANY(...).

        Returns list of dicts: input_designation, designation, id, match_type.
        """
        if not designation_names:
            return []

        rows = (
            await db.execute(
                text(
                    "SELECT id, designation FROM designation_embeddings "
                    "WHERE LOWER(designation) = ANY(:names)"
                ),
                {"names": [n.lower() for n in designation_names]},
            )
        ).fetchall()

        matched = {row.designation.lower(): row for row in rows}
        results = [
            {
                "input_designation": name,
                "designation": matched[name.lower()].designation,
                "id": matched[name.lower()].id,
                "match_type": "exact",
            }
            for name in designation_names
            if name.lower() in matched
        ]

        logger.info(f"Exact match: {len(results)}/{len(designation_names)} found")
        return results

    async def match_semantic(
        self, db: AsyncSession, designation_names: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Semantic match using gemini-embedding-2 + pgvector cosine similarity.
        Only returns matches with similarity >= SIMILARITY_THRESHOLD (0.92).

        Returns list of dicts: input_designation, designation, id, similarity_score, match_type.
        """
        if not designation_names:
            return []

        logger.info(f"Generating embeddings for {len(designation_names)} designation(s) via {settings.GOOGLE_EMBEDDING_MODEL}")
        embeddings = await _get_embeddings(designation_names)

        results = []
        for name, emb in zip(designation_names, embeddings):
            emb_str = "[" + ",".join(f"{x:.6f}" for x in emb) + "]"

            row = (
                await db.execute(
                    text(f"""
                        SELECT id, designation,
                               1.0 - (embedding <=> '{emb_str}'::vector) AS similarity_score
                        FROM designation_embeddings
                        ORDER BY embedding <=> '{emb_str}'::vector
                        LIMIT 1
                    """)
                )
            ).fetchone()

            if row:
                score = float(row.similarity_score)
                if score >= settings.DESIGNATION_SIMILARITY_THRESHOLD:
                    results.append({
                        "input_designation": name,
                        "designation": row.designation,
                        "id": row.id,
                        "similarity_score": round(score, 4),
                        "match_type": "semantic",
                    })

        logger.info(f"Semantic match: {len(results)}/{len(designation_names)} found above threshold {settings.DESIGNATION_SIMILARITY_THRESHOLD}")
        return results

    async def match(
        self, db: AsyncSession, designation_names: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Full match pipeline:
          1. Exact match for all names.
          2. Semantic match for anything not exact-matched.

        Returns combined results from both strategies.
        """
        if not designation_names:
            return []

        exact_results = await self.match_exact(db, designation_names)
        exact_dict    = {m["input_designation"].lower(): m for m in exact_results}

        unmatched = [n for n in designation_names if n.lower() not in exact_dict]
        semantic_results = await self.match_semantic(db, unmatched) if unmatched else []

        return exact_results + semantic_results


designation_matcher_service = DesignationMatcherService()

from typing import Any, Dict, List

import httpx
from sentence_transformers import SentenceTransformer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from ..core.configs import settings
from ..core.logger import logger

#  load once at startup so every API request doesn't pay the load cost
embedding_model = SentenceTransformer("BAAI/bge-base-en-v1.5")

SIMILARITY_THRESHOLD = 0.92  # cosine similarity; translates to distance <= 0.08


class DesignationService:
    """Handles all iGOT designation-related API calls."""

    DESIGNATION_SEARCH = "/api/designation/search"

    def __init__(self):
        self.base_url = settings.KB_BASE_URL
        self.auth_token = settings.KB_AUTH_TOKEN
        self.timeout = 30.0

    def _get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"{self.auth_token}",
        }

    async def search(self, body: dict) -> dict:
        """Proxy search request to iGOT designation API."""
        url = f"{self.base_url}{self.DESIGNATION_SEARCH}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
            )
            response.raise_for_status()
            return response.json()
        
    async def match_designations(self, designation_names: List[str]) -> List[Dict[str, Any]]:
        """
        Match a list of designation names against the iGOT designation search API.

        Returns a list of result dicts, one per input name, each with:
            - igot_designation_name: the original input name
            - matched_designation: the exact name from iGOT (or None)
            - igot_id: the iGOT ID string (or None)
            - similarity_score: 1.0 if matched, 0.0 if not
            - is_matched: True / False
        """
        if not designation_names:
            return []

        payload = {
            "filterCriteriaMap": {
                "status": "Active",
                "designation": designation_names
            },
            "requestedFields": ["designation", "id"],
            "pageNumber": 0,
            "pageSize": max(len(designation_names), 1000)
        }

        logger.info(f"Calling iGOT designation search API for {len(designation_names)} designation(s)")

        data = await self.search(payload)

        designations = data.get("result", {}).get("result", {}).get("data", [])
        logger.info(f"iGOT API matched {len(designations)}/{len(designation_names)} designation(s)")
        return designations

    async def match_designations_via_embedding(
        self, db: AsyncSession, designation_names: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Match designation names against the designation_embeddings table using
        pgvector cosine similarity. Only returns matches >= SIMILARITY_THRESHOLD (0.92).

        Returns list of dicts with:
            - input_designation : the name that was searched
            - designation       : matched name from DB
            - id                : iGOT designation ID from DB
        """
        if not designation_names:
            return []

        # cosine similarity >= 0.92  =>  distance <= 0.08
        max_distance = 1.0 - SIMILARITY_THRESHOLD

        embeddings = embedding_model.encode(
            designation_names,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        results = []
        for name, emb in zip(designation_names, embeddings):
            emb_str = "[" + ",".join(f"{x:.6f}" for x in emb.tolist()) + "]"

            row = (
                await db.execute(
                    #  emb_str is numpy floats we built — safe to interpolate
                    # asyncpg chokes on :param::vector, so we inline the literal
                    text(f"""
                        SELECT id, designation, 1.0 - (embedding <=> '{emb_str}'::vector) AS similarity_score
                        FROM designation_embeddings
                        ORDER BY embedding <=> '{emb_str}'::vector
                        LIMIT 1
                    """)
                )
            ).fetchone()

            if row:
                score = float(row.similarity_score)
                if score >= SIMILARITY_THRESHOLD:
                    results.append({
                        "input_designation": name,
                        "designation": row.designation,
                        "id": row.id,
                        "similarity_score": score,
                    })

        return results


designation_service = DesignationService()

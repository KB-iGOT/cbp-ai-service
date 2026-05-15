from typing import Any, Dict, List

import httpx
from ..core.configs import settings
from ..core.logger import logger


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
        logger.info(f"Searching designations at {url} with body: {body}")

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


designation_service = DesignationService()

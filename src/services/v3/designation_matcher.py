from typing import Any, Dict, List

import httpx

from ...core.configs import settings
from ...core.logger import logger


class DesignationMatcher:
    """Service class for matching AI-generated designations against the iGOT Designation Master."""

    def __init__(self, auth_token: str = None):
        self.auth_token = auth_token or settings.KB_AUTH_TOKEN
        self.base_url = settings.KB_BASE_URL

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

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.base_url}/api/designation/search",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.auth_token}"
                }
            )
            response.raise_for_status()
            data = response.json()

        api_data = data.get("result", {}).get("result", {}).get("data", [])
        matched_map: Dict[str, Dict] = {
            item["designation"]: item
            for item in api_data
            if item.get("designation")
        }

        results: List[Dict[str, Any]] = []
        for name in designation_names:
            if name in matched_map:
                results.append({
                    "igot_designation_name": name,
                    "matched_designation": matched_map[name]["designation"],
                    "igot_id": str(matched_map[name].get("id", "")),
                    "is_matched": True
                })
            else:
                results.append({
                    "igot_designation_name": name,
                    "matched_designation": None,
                    "igot_id": None,
                    "is_matched": False
                })

        matched_count = sum(1 for r in results if r["is_matched"])
        logger.info(f"iGOT API matched {matched_count}/{len(designation_names)} designation(s)")
        return results

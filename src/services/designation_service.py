import httpx

from ..core.configs import settings


class DesignationService:
    """Handles all iGOT designation-related API calls."""

    DESIGNATION_SEARCH = "/api/designation/search"

    def __init__(self):
        self.base_url   = settings.KB_BASE_URL
        self.auth_token = settings.KB_AUTH_TOKEN
        self.timeout    = 30.0

    def _get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"{self.auth_token}",
        }

    async def search(self, body: dict) -> dict:
        """Proxy search request to iGOT designation API."""
        url = f"{self.base_url}{self.DESIGNATION_SEARCH}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=body, headers=self._get_headers())
            response.raise_for_status()
            return response.json()


designation_service = DesignationService()

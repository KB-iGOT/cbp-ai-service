import httpx
from ..core.configs import settings
from ..core.logger import logger


class DesignationService:
    """Handles all iGOT designation-related API calls."""

    BASE_PATH = "/apis/public/v8/designation"

    def __init__(self):
        self.base_url = settings.KB_BASE_URL
        self.auth_token = settings.KB_AUTH_TOKEN
        self.timeout = 30.0

    def _get_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }

    async def search(self, body: dict) -> dict:
        """Proxy search request to iGOT designation API."""
        url = f"{self.base_url}{self.BASE_PATH}/search"
        logger.info(f"Searching designations at {url} with body: {body}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                json=body,
                headers=self._get_headers(),
            )
            response.raise_for_status()
            return response.json()


designation_service = DesignationService()

from datetime import UTC, datetime

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core.configs import settings

router = APIRouter(tags=["health"])

STATUS_HEALTHY = "healthy"
STATUS_UNHEALTHY = "unhealthy"

class HealthCheck(BaseModel):
    status: str
    # environment: str
    version: str
    timestamp: str

@router.get("/health", response_model=HealthCheck)
async def health():
    http_status = status.HTTP_200_OK
    response = {
        "status": STATUS_HEALTHY,
        # "environment": settings.ENVIRONMENT.value,
        "version": settings.APP_VERSION,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    return JSONResponse(status_code=http_status, content=response)
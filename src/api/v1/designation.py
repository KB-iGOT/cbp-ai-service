from fastapi import APIRouter, Body, Depends, HTTPException, status

from ...models.user import User
from ...api.dependencies import get_current_active_user
from ...core.logger import logger
from ...services.designation_service import designation_service

router = APIRouter(tags=["Designations"])


@router.post(
    "/kb/designation/search",
    status_code=status.HTTP_200_OK,
)
async def search_designations(
    body: dict = Body(default={"filterCriteriaMap":{"status":"Active"},"requestedFields":[],"pageNumber":0,"pageSize":50}),
    current_user: User = Depends(get_current_active_user),
):
    """
    Search iGOT designations via the Karmayogi Bharat portal API.
    """
    try:
        logger.info(f"Searching designations with body: {body}")
        return await designation_service.search(body)
    except Exception as e:
        logger.exception("Unexpected error while searching designations:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch designations from iGOT portal",
        )

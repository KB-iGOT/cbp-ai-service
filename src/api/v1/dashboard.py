from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.crud.dashboard import DashboardQueryError, InvalidTrendGranularity

from ...models.user import User

from ...schemas.dashboard import CBPSummaryTrendRequest, CBPSummaryTrendResponse

from ...api.dependencies import require_role
from ...core.database import get_db_session
from ...crud.dashboard import crud_dashboard

from ...core.logger import logger

router = APIRouter(prefix="/dashboard",tags=["Dashboard"])

# Dashboard APIs
@router.post("/cbp-summary-trends", response_model=list[CBPSummaryTrendResponse], status_code=status.HTTP_200_OK)
async def cbp_summary_trends(
    request: CBPSummaryTrendRequest,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    logger.info(f"Recieved request for CBP summary: {request.model_dump()}")
    try:
        return await crud_dashboard.fetch_cbp_summary_trends(db, request.filters)
    except InvalidTrendGranularity as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e)
        )
    except DashboardQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Error while generating CBP summary trends:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error occurred while generating CBP summary trends"
        )
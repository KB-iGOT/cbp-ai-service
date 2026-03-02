from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.crud.dashboard import DashboardQueryError, InvalidTrendGranularity

from ...models.user import User

from ...schemas.dashboard import (
    CBPSummaryTrendRequest, CBPSummaryTrendResponse,
    CBPDashboardFilters, CBPDashboardMetricsResponse,
    GapAnalysisFilters, GapAnalysisResponse,
    UserDashboardFilters, UserDashboardMetricsResponse, UserGapAnalysisResponse
)

from ...api.dependencies import require_role, get_current_active_user
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
    logger.info(f"Received request for CBP summary: {request.model_dump()}")
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

@router.post("/cbp-dashboard-metrics", response_model=CBPDashboardMetricsResponse, status_code=status.HTTP_200_OK)
async def get_cbp_dashboard_metrics(
    filters: CBPDashboardFilters,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    logger.info(f"Received request for CBP dashboard metrics: {filters.model_dump()}")
    try:
        return await crud_dashboard.fetch_dashboard_metrics(db, filters)
    except DashboardQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Error while fetching CBP dashboard metrics:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error occurred while fetching CBP dashboard metrics"
        )

@router.post("/gap-analysis", response_model=GapAnalysisResponse, status_code=status.HTTP_200_OK)
async def get_gap_analysis(
    filters: GapAnalysisFilters,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(require_role("Super Admin"))
):
    logger.info(f"Received request for gap analysis: {filters.model_dump()}")
    try:
        return await crud_dashboard.fetch_gap_analysis(db, filters)
    except DashboardQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Error while computing gap analysis:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error occurred while computing gap analysis"
        )


# ── User-scoped endpoints (accessible to any authenticated user) ──────────────

@router.post("/my-dashboard-metrics", response_model=UserDashboardMetricsResponse, status_code=status.HTTP_200_OK)
async def get_my_dashboard_metrics(
    filters: UserDashboardFilters,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    logger.info(f"Received request for user dashboard metrics: user_id={current_user.user_id}")
    try:
        return await crud_dashboard.fetch_dashboard_metrics(db, filters, user_id=current_user.user_id)
    except DashboardQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Error while fetching user dashboard metrics:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error occurred while fetching your dashboard metrics"
        )


@router.post("/my-gap-analysis", response_model=UserGapAnalysisResponse, status_code=status.HTTP_200_OK)
async def get_my_gap_analysis(
    filters: UserDashboardFilters,
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user)
):
    logger.info(f"Received request for user gap analysis: user_id={current_user.user_id}")
    try:
        return await crud_dashboard.fetch_gap_analysis(db, filters, user_id=current_user.user_id)
    except DashboardQueryError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.exception("Error while computing user gap analysis:")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error occurred while computing your gap analysis"
        )
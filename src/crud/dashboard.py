from sqlalchemy import Integer, String, select, func, text
from sqlalchemy.exc import SQLAlchemyError
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.cbp_plan import CBPPlan
from ..models.role_mapping import RoleMapping

from ..schemas.dashboard import CBPSummaryTrendFilters


class InvalidTrendGranularity(Exception):
    pass

class DashboardQueryError(Exception):
    pass

def get_period_expression(granularity: str):
    if granularity == "Monthly":
        return func.to_char(
            func.date_trunc("month", CBPPlan.created_at),
            "YYYY-MM"
        )
    elif granularity == "Quarterly":
        return (
            func.to_char(func.date_trunc("quarter", CBPPlan.created_at), "YYYY")
            + "-Q"
            + func.extract("quarter", CBPPlan.created_at)
              .cast(Integer)      # <-- Fix: Use Integer type, not text("int")
              .cast(String)       # <-- Fix: Use String type, not text("text")
        )
    else:
        raise InvalidTrendGranularity(
            "trend_granularity must be 'Monthly' or 'Quarterly'"
        )


class CRUDDashboard:
    """
    CRUD methods for the Document model.
    """

    async def fetch_cbp_summary_trends(
        self,
        db: AsyncSession,
        filters: CBPSummaryTrendFilters
    ):
        try:
            period_expr = get_period_expression(filters.trend_granularity)

            stmt = (
                select(
                    RoleMapping.state_center_id,
                    RoleMapping.state_center_name,
                    RoleMapping.department_name.label("department_org_name"),
                    period_expr.label("period"),
                    func.count(CBPPlan.id).label("cbp_count"),
                )
                .join(CBPPlan, CBPPlan.role_mapping_id == RoleMapping.id)
            )

            # ---- Date filter (optional) ----
            if filters.date_range:
                stmt = stmt.where(
                    CBPPlan.created_at >= filters.date_range.from_date,
                    CBPPlan.created_at <= filters.date_range.to_date,
                )

            if filters.state_center_id:
                stmt = stmt.where(
                    RoleMapping.state_center_id == filters.state_center_id
                )

            if filters.department_org_ids:
                stmt = stmt.where(
                    RoleMapping.department_id.in_(filters.department_org_ids)
                )

            stmt = stmt.group_by(
                RoleMapping.state_center_id,
                RoleMapping.state_center_name,
                RoleMapping.department_name,
                period_expr,
            )

            result = await db.execute(stmt)
            rows = result.fetchall()

            # -------- Transform response --------
            response_map = defaultdict(list)

            for row in rows:
                key = (
                    row.state_center_id,
                    row.state_center_name,
                    row.department_org_name,
                )
                response_map[key].append({
                    "period": row.period,
                    "cbp_count": row.cbp_count
                })

            return [
                {
                    "state_center_id": state_id,
                    "state_center_name": state_name,
                    "department_org_name": dept_name,
                    "trend": trends
                }
                for (state_id, state_name, dept_name), trends in response_map.items()
            ]

        except SQLAlchemyError as e:
            print(f"Database error while fetching CBP summary trends : {str(e)}")
            await db.rollback()
            raise DashboardQueryError(
                "Database error while fetching CBP summary trends"
            ) from e
    
# Initialize the CRUD utility for use across the application
crud_dashboard = CRUDDashboard()
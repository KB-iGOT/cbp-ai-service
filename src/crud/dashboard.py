from sqlalchemy import Integer, String, select, func, text, case
from sqlalchemy.dialects.postgresql import array as pg_array
from sqlalchemy.exc import SQLAlchemyError
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.cbp_plan import CBPPlan
from ..models.role_mapping import RoleMapping
from ..models.course_recommendation import RecommendedCourse
from ..models.document import Document
from ..models.user import User

from ..schemas.dashboard import CBPSummaryTrendFilters, CBPDashboardFilters, GapAnalysisFilters


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
              .cast(Integer)
              .cast(String)
        )
    else:
        raise InvalidTrendGranularity(
            "trend_granularity must be 'Monthly' or 'Quarterly'"
        )


class CRUDDashboard:
    """
    CRUD methods for the Dashboard model.
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

            if filters.date_range:
                stmt = stmt.where(
                    func.date(CBPPlan.created_at) >= filters.date_range.from_date,
                    func.date(CBPPlan.created_at) <= filters.date_range.to_date,
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

    # ── Super Admin methods ───────────────────────────────────────────────────

    async def fetch_cbp_dashboard_metrics(
        self,
        db: AsyncSession,
        filters: CBPDashboardFilters
    ):
        try:
            stmt = select(
                func.count(func.distinct(RoleMapping.id)).label("total_role_mappings"),
                func.count(func.distinct(func.lower(RoleMapping.designation_name))).label("unique_role_mappings"),
                func.count(func.distinct(RoleMapping.user_id)).label("users_with_role_mappings"),
                func.count(func.distinct(RoleMapping.state_center_id)).label("ministry_count"),
                func.count(func.distinct(RoleMapping.department_id)).label("department_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Behavioral'"))
                    .scalar_subquery()
                ).label("behavioral_competency_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Functional'"))
                    .scalar_subquery()
                ).label("functional_competency_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Domain'"))
                    .scalar_subquery()
                ).label("domain_competency_count")
            )

            if filters.ministries and len(filters.ministries) > 0:
                stmt = stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                stmt = stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    stmt = stmt.where(func.date(RoleMapping.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    stmt = stmt.where(func.date(RoleMapping.created_at) <= filters.date_range.to_date)

            stmt = stmt.where(RoleMapping.status == 'COMPLETED')

            result = await db.execute(stmt)
            row = result.fetchone()

            doc_stmt = select(func.count(func.distinct(Document.file_id)))
            if filters.ministries and len(filters.ministries) > 0:
                doc_stmt = doc_stmt.where(Document.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                doc_stmt = doc_stmt.where(Document.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    doc_stmt = doc_stmt.where(func.date(Document.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    doc_stmt = doc_stmt.where(func.date(Document.created_at) <= filters.date_range.to_date)

            doc_result = await db.execute(doc_stmt)
            total_documents = doc_result.scalar() or 0

            cbp_stmt = select(func.count(func.distinct(CBPPlan.id)))
            cbp_stmt = cbp_stmt.join(RoleMapping, RoleMapping.id == CBPPlan.role_mapping_id)

            if filters.ministries and len(filters.ministries) > 0:
                cbp_stmt = cbp_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                cbp_stmt = cbp_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    cbp_stmt = cbp_stmt.where(func.date(CBPPlan.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    cbp_stmt = cbp_stmt.where(func.date(CBPPlan.created_at) <= filters.date_range.to_date)

            cbp_result = await db.execute(cbp_stmt)
            total_cbp_plan_count = cbp_result.scalar() or 0

            rec_stmt = select(func.count(func.distinct(RecommendedCourse.role_mapping_id)))
            rec_stmt = rec_stmt.join(RoleMapping, RoleMapping.id == RecommendedCourse.role_mapping_id)

            if filters.ministries and len(filters.ministries) > 0:
                rec_stmt = rec_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                rec_stmt = rec_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    rec_stmt = rec_stmt.where(func.date(RoleMapping.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    rec_stmt = rec_stmt.where(func.date(RoleMapping.created_at) <= filters.date_range.to_date)

            rec_stmt = rec_stmt.where(RoleMapping.status == 'COMPLETED')

            rec_result = await db.execute(rec_stmt)
            role_mappings_with_recommendations = rec_result.scalar() or 0

            # --- Total Users Query ---
            # Count directly from the users table.
            # Ministry filter uses organization_ids (ARRAY) which stores ministry/state IDs.
            # Department filter is not applied here — organization_ids has no department IDs.
            user_stmt = select(func.count(func.distinct(User.user_id)))

            if filters.ministries and len(filters.ministries) > 0:
                user_stmt = user_stmt.where(
                    User.organization_ids.op('&&')(pg_array(filters.ministries))
                )

            if filters.date_range:
                if filters.date_range.from_date:
                    user_stmt = user_stmt.where(func.date(User.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    user_stmt = user_stmt.where(func.date(User.created_at) <= filters.date_range.to_date)

            user_result = await db.execute(user_stmt)
            total_users = user_result.scalar() or 0

            # --- Saved Recommended Courses Count ---
            saved_rec_stmt = select(
                func.sum(
                    select(func.count(func.distinct(text("s->>'identifier'"))))
                    .select_from(
                        func.jsonb_array_elements(CBPPlan.selected_courses).alias("s")
                    )
                    .where(
                        text("s->>'identifier' IN (SELECT r->>'identifier' FROM jsonb_array_elements(recommended_courses.filtered_courses) AS r)")
                    )
                    .scalar_subquery()
                )
            )
            saved_rec_stmt = saved_rec_stmt.select_from(CBPPlan)
            saved_rec_stmt = saved_rec_stmt.join(RoleMapping, RoleMapping.id == CBPPlan.role_mapping_id)
            saved_rec_stmt = saved_rec_stmt.join(RecommendedCourse, RecommendedCourse.id == CBPPlan.recommended_course_id)
            saved_rec_stmt = saved_rec_stmt.where(CBPPlan.recommended_course_id.is_not(None))

            if filters.ministries and len(filters.ministries) > 0:
                saved_rec_stmt = saved_rec_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                saved_rec_stmt = saved_rec_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    saved_rec_stmt = saved_rec_stmt.where(func.date(CBPPlan.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    saved_rec_stmt = saved_rec_stmt.where(func.date(CBPPlan.created_at) <= filters.date_range.to_date)

            saved_rec_result = await db.execute(saved_rec_stmt)
            saved_recommended_courses_count = saved_rec_result.scalar() or 0

            print(f"[CBP Dashboard Metrics] total_users={total_users}, total_role_mappings={row.total_role_mappings if row else 0}")
            return {
                "total_users": total_users,
                "users_with_role_mappings": row.users_with_role_mappings if row else 0,
                "total_role_mappings": row.total_role_mappings if row else 0,
                "unique_role_mappings": row.unique_role_mappings if row else 0,
                "role_mappings_with_recommendations": role_mappings_with_recommendations,
                "saved_recommended_courses_count": int(saved_recommended_courses_count),
                "ministry_count": row.ministry_count if row else 0,
                "department_count": row.department_count if row else 0,
                "total_documents": total_documents,
                "total_cbp_plan_count": total_cbp_plan_count,
                "behavioral_competencies_count": row.behavioral_competency_count if row and row.behavioral_competency_count else 0,
                "functional_competencies_count": row.functional_competency_count if row and row.functional_competency_count else 0,
                "domain_competencies_count": row.domain_competency_count if row and row.domain_competency_count else 0
            }

        except SQLAlchemyError as e:
            print(f"Database error while fetching role mapping count: {str(e)}")
            await db.rollback()
            raise DashboardQueryError("Database error while fetching role mapping count") from e

    async def fetch_gap_analysis(
        self,
        db: AsyncSession,
        filters,
        user_id=None
    ):
        """
        Fetch gap analysis data.

        - If user_id is None  → Super Admin path: considers all role mappings.
        - If user_id is given → User path: scoped to that user's role mappings only.
        """
        try:
            where_clauses = ["rm.status = 'COMPLETED'"]
            params = {}

            # Scope to a specific user when called from the user-facing endpoint.
            if user_id is not None:
                where_clauses.append("rm.user_id = :user_id")
                params["user_id"] = str(user_id)

            if filters.ministries and len(filters.ministries) > 0:
                where_clauses.append("rm.state_center_id = ANY(:ministries)")
                params["ministries"] = filters.ministries

            if filters.departments and len(filters.departments) > 0:
                where_clauses.append("rm.department_id = ANY(:departments)")
                params["departments"] = filters.departments

            if filters.date_range:
                if filters.date_range.from_date:
                    where_clauses.append("DATE(rm.created_at) >= :from_date")
                    params["from_date"] = filters.date_range.from_date
                if filters.date_range.to_date:
                    where_clauses.append("DATE(rm.created_at) <= :to_date")
                    params["to_date"] = filters.date_range.to_date

            where_sql = " AND ".join(where_clauses)

            raw_sql = text(f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE REPLACE(LOWER(TRIM(c->>'type')), 'behavioural', 'behavioral') = 'behavioral'
                    ) AS behavioral_without_courses,
                    COUNT(*) FILTER (
                        WHERE LOWER(TRIM(c->>'type')) = 'functional'
                    ) AS functional_without_courses,
                    COUNT(*) FILTER (
                        WHERE LOWER(TRIM(c->>'type')) = 'domain'
                    ) AS domain_without_courses
                FROM role_mappings rm
                JOIN recommended_courses rc2 ON rc2.role_mapping_id = rm.id
                JOIN LATERAL jsonb_array_elements(
                    COALESCE(rm.competencies, '[]'::jsonb)
                ) AS c ON true
                WHERE {where_sql}
                AND NOT EXISTS (
                    SELECT 1
                    FROM recommended_courses rc
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(rc.filtered_courses) = 'array'
                            THEN rc.filtered_courses ELSE '[]'::jsonb END
                    ) AS fc
                    CROSS JOIN LATERAL jsonb_array_elements(
                        CASE WHEN jsonb_typeof(fc->'competencies') = 'array'
                            THEN fc->'competencies' ELSE '[]'::jsonb END
                    ) AS course_comp
                    WHERE rc.role_mapping_id = rm.id
                    AND REPLACE(LOWER(TRIM(course_comp->>'competencyAreaName')), 'behavioural', 'behavioral')
                        = REPLACE(LOWER(TRIM(c->>'type')), 'behavioural', 'behavioral')
                    AND LOWER(TRIM(course_comp->>'competencyThemeName')) = LOWER(TRIM(c->>'theme'))
                    AND LOWER(TRIM(course_comp->>'competencySubThemeName')) = LOWER(TRIM(c->>'sub_theme'))
                )
            """)

            result = await db.execute(raw_sql, params)
            row = result.fetchone()

            behavioral = row.behavioral_without_courses if row and row.behavioral_without_courses else 0
            functional = row.functional_without_courses if row and row.functional_without_courses else 0
            domain = row.domain_without_courses if row and row.domain_without_courses else 0

            return {
                "competencies_without_courses": behavioral + functional + domain,
                "behavioral_without_courses": behavioral,
                "functional_without_courses": functional,
                "domain_without_courses": domain
            }

        except SQLAlchemyError as e:
            print(f"Database error while fetching gap analysis: {str(e)}")
            await db.rollback()
            raise DashboardQueryError(
                "Database error while fetching gap analysis"
            ) from e

    # ── User-scoped methods (for regular/public users) ────────────────────────

    async def fetch_user_dashboard_metrics(
        self,
        db: AsyncSession,
        user_id,
        filters
    ):
        """Returns CBP dashboard metrics scoped to a single user's data."""
        try:
            uid = str(user_id)

            stmt = select(
                func.count(func.distinct(RoleMapping.id)).label("total_role_mappings"),
                func.count(func.distinct(func.lower(RoleMapping.designation_name))).label("unique_role_mappings"),
                func.count(func.distinct(RoleMapping.state_center_id)).label("ministry_count"),
                func.count(func.distinct(RoleMapping.department_id)).label("department_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Behavioral'"))
                    .scalar_subquery()
                ).label("behavioral_competency_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Functional'"))
                    .scalar_subquery()
                ).label("functional_competency_count"),
                func.sum(
                    select(func.count())
                    .select_from(func.jsonb_array_elements(
                        case(
                            (func.jsonb_typeof(RoleMapping.competencies) == 'array', RoleMapping.competencies),
                            else_=text("'[]'::jsonb")
                        )
                    ).alias("c"))
                    .where(text("c->>'type' = 'Domain'"))
                    .scalar_subquery()
                ).label("domain_competency_count"),
            ).where(
                RoleMapping.user_id == user_id,
                RoleMapping.status == 'COMPLETED'
            )

            if filters.ministries and len(filters.ministries) > 0:
                stmt = stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                stmt = stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    stmt = stmt.where(func.date(RoleMapping.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    stmt = stmt.where(func.date(RoleMapping.created_at) <= filters.date_range.to_date)

            result = await db.execute(stmt)
            row = result.fetchone()

            rec_stmt = (
                select(func.count(func.distinct(RecommendedCourse.role_mapping_id)))
                .join(RoleMapping, RoleMapping.id == RecommendedCourse.role_mapping_id)
                .where(RoleMapping.user_id == user_id, RoleMapping.status == 'COMPLETED')
            )
            if filters.ministries and len(filters.ministries) > 0:
                rec_stmt = rec_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                rec_stmt = rec_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    rec_stmt = rec_stmt.where(func.date(RoleMapping.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    rec_stmt = rec_stmt.where(func.date(RoleMapping.created_at) <= filters.date_range.to_date)

            rec_result = await db.execute(rec_stmt)
            role_mappings_with_recommendations = rec_result.scalar() or 0

            cbp_stmt = (
                select(func.count(func.distinct(CBPPlan.id)))
                .join(RoleMapping, RoleMapping.id == CBPPlan.role_mapping_id)
                .where(CBPPlan.user_id == user_id)
            )
            if filters.ministries and len(filters.ministries) > 0:
                cbp_stmt = cbp_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                cbp_stmt = cbp_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    cbp_stmt = cbp_stmt.where(func.date(CBPPlan.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    cbp_stmt = cbp_stmt.where(func.date(CBPPlan.created_at) <= filters.date_range.to_date)

            cbp_result = await db.execute(cbp_stmt)
            total_cbp_plan_count = cbp_result.scalar() or 0

            saved_rec_stmt = select(
                func.sum(
                    select(func.count(func.distinct(text("s->>'identifier'"))))
                    .select_from(func.jsonb_array_elements(CBPPlan.selected_courses).alias("s"))
                    .where(
                        text("s->>'identifier' IN (SELECT r->>'identifier' FROM jsonb_array_elements(recommended_courses.filtered_courses) AS r)")
                    )
                    .scalar_subquery()
                )
            )
            saved_rec_stmt = saved_rec_stmt.select_from(CBPPlan)
            saved_rec_stmt = saved_rec_stmt.join(RoleMapping, RoleMapping.id == CBPPlan.role_mapping_id)
            saved_rec_stmt = saved_rec_stmt.join(RecommendedCourse, RecommendedCourse.id == CBPPlan.recommended_course_id)
            saved_rec_stmt = saved_rec_stmt.where(
                CBPPlan.recommended_course_id.is_not(None),
                CBPPlan.user_id == user_id
            )
            if filters.ministries and len(filters.ministries) > 0:
                saved_rec_stmt = saved_rec_stmt.where(RoleMapping.state_center_id.in_(filters.ministries))

            if filters.departments and len(filters.departments) > 0:
                saved_rec_stmt = saved_rec_stmt.where(RoleMapping.department_id.in_(filters.departments))

            if filters.date_range:
                if filters.date_range.from_date:
                    saved_rec_stmt = saved_rec_stmt.where(func.date(CBPPlan.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    saved_rec_stmt = saved_rec_stmt.where(func.date(CBPPlan.created_at) <= filters.date_range.to_date)

            saved_rec_result = await db.execute(saved_rec_stmt)
            saved_recommended_courses_count = saved_rec_result.scalar() or 0

            # --- Total Documents uploaded by this user ---
            doc_stmt = select(func.count(func.distinct(Document.file_id))).where(
                Document.uploader_id == user_id
            )
            if filters.date_range:
                if filters.date_range.from_date:
                    doc_stmt = doc_stmt.where(func.date(Document.created_at) >= filters.date_range.from_date)
                if filters.date_range.to_date:
                    doc_stmt = doc_stmt.where(func.date(Document.created_at) <= filters.date_range.to_date)
            doc_result = await db.execute(doc_stmt)
            total_documents = doc_result.scalar() or 0

            print(f"[User Dashboard Metrics] user_id={uid}, total_role_mappings={row.total_role_mappings if row else 0}")
            return {
                "total_role_mappings": row.total_role_mappings if row else 0,
                "unique_role_mappings": row.unique_role_mappings if row else 0,
                "role_mappings_with_recommendations": role_mappings_with_recommendations,
                "saved_recommended_courses_count": int(saved_recommended_courses_count),
                "ministry_count": row.ministry_count if row else 0,
                "department_count": row.department_count if row else 0,
                "total_documents": total_documents,
                "total_cbp_plan_count": total_cbp_plan_count,
                "behavioral_competencies_count": row.behavioral_competency_count if row and row.behavioral_competency_count else 0,
                "functional_competencies_count": row.functional_competency_count if row and row.functional_competency_count else 0,
                "domain_competencies_count": row.domain_competency_count if row and row.domain_competency_count else 0,
            }

        except SQLAlchemyError as e:
            print(f"Database error while fetching user dashboard metrics: {str(e)}")
            await db.rollback()
            raise DashboardQueryError("Database error while fetching user dashboard metrics") from e

    # fetch_user_gap_analysis removed — merged into fetch_gap_analysis(user_id=...) above.


# Initialize the CRUD utility for use across the application
crud_dashboard = CRUDDashboard()
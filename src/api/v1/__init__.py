
from fastapi import APIRouter

from .auth import router as auth_router
from .cbp_plan import router as cbp_plan_router
from .course_recommendation import router as course_recommendation_router
from .course_suggestion import router as course_suggestion_router
from .department import router as department_router
from .role_mappings import router as role_mappings_router
from .roles import router as roles_router
from .users import router as users_router
from .state_center_data import router as state_center_data_router
from .state_center import router as state_center_router
from .user_added_courses import router as user_added_courses_router
from .users import router as users_router
from .document_routes import router as document_routes_new
from .meta_summary_routes import router as meta_summary_routes_new
from .health import router as health_routes
from .dashboard import router as dashboard_routes

router = APIRouter(prefix="/v1")

router.include_router(auth_router)
router.include_router(state_center_router)
router.include_router(department_router)
router.include_router(state_center_data_router)
router.include_router(role_mappings_router)
router.include_router(course_recommendation_router)
router.include_router(course_suggestion_router)
router.include_router(user_added_courses_router)
router.include_router(cbp_plan_router)
router.include_router(roles_router)
router.include_router(users_router)
router.include_router(document_routes_new)
router.include_router(meta_summary_routes_new)
router.include_router(dashboard_routes)
router.include_router(health_routes)
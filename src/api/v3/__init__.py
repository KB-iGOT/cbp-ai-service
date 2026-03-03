
from fastapi import APIRouter
from .role_mappings import router as role_mappings_router

router = APIRouter(prefix="/v3")

router.include_router(role_mappings_router)
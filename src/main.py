from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.database import Base, sessionmanager
from .api import router
from .core.configs import EnvironmentOption, settings
from .core.logger import logger

# Import all models to ensure they're registered with Base.metadata
from .models import approval_request  # noqa: F401

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("✅ Starting up...")
    
    sessionmanager.init(settings.DATABASE_URL)
    
    logger.info("--- Creating Tables ---")
    async with sessionmanager.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("✅ Database tables ready")
    
    yield
    # On shutdown, dispose of the connection pool
    logger.info("🔻 Shutting down...")
    await sessionmanager.close()
    logger.info("🔻 DB connection closed")


app = FastAPI(
    root_path= settings.APP_ROOT_PATH,
    title=settings.APP_NAME,
    description=settings.APP_DESC,
    version=settings.APP_VERSION,
    docs_url = None if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION else "/docs",
    redoc_url = None if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION else "/redoc",
    openapi_url = None if settings.ENVIRONMENT == EnvironmentOption.PRODUCTION else "/openapi.json",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Allow all origins
    allow_credentials=True,  # Must be False when using "*"
    allow_methods=["*"],      # Allow all HTTP methods
    allow_headers=["*"],      # Allow all headers
)

app.include_router(router)

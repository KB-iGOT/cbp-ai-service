from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.database import Base, sessionmanager
from .api import router
from .core.configs import settings
from .core.logger import logger

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("✅ Starting up...")
    
    sessionmanager.init(settings.DATABASE_URL)
    
    print("--- Creating Tables ---")
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
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Allow all origins
    allow_credentials=True,  # Must be False when using "*"
    allow_methods=["*"],      # Allow all HTTP methods
    allow_headers=["*"],      # Allow all headers
)

@app.get("/")
def read_root():
    return {"message": "Welcome to the AI-Driven CBP Training Plan Creation System!"}

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check requested")
    return {"status": "healthy"}

app.include_router(router)

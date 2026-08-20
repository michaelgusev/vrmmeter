from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI
import uvicorn

from vrmmeter.config import settings
from vrmmeter.db.session import init_db
from vrmmeter.services.scheduler import shutdown_scheduler, start_scheduler
from vrmmeter.web.api import api_router
from vrmmeter.web.views import view_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vrmmeter")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize Database tables
    logger.info("Initializing VRM Meter database...")
    await init_db()
    logger.info("Database initialized successfully.")

    # Startup: Background Autonomous Scheduler
    if settings.scheduler_enabled:
        logger.info("Starting background autonomous sync scheduler...")
        start_scheduler()

    yield

    # Shutdown: Stop scheduler
    if settings.scheduler_enabled:
        logger.info("Shutting down background scheduler...")
        shutdown_scheduler()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Autonomous power consumption estimation, billing, and physical meter estimation for Victron VRM installations.",
    lifespan=lifespan,
)

# Register routers
app.include_router(api_router)
app.include_router(view_router)


if __name__ == "__main__":
    uvicorn.run(
        "vrmmeter.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )

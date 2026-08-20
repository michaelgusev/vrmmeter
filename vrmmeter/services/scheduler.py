import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from vrmmeter.config import settings
from vrmmeter.db.session import async_session_factory
from vrmmeter.services.sync_service import sync_all_active_installations

logger = logging.getLogger("vrmmeter.scheduler")

scheduler: AsyncIOScheduler | None = None


async def _run_periodic_sync():
    """Job executed periodically by APScheduler."""
    logger.info("Starting scheduled VRM synchronization...")
    try:
        async with async_session_factory() as session:
            count = await sync_all_active_installations(session)
            logger.info(f"Scheduled sync complete: {count} records synced across active installations.")
    except Exception as e:
        logger.error(f"Error during scheduled VRM sync: {e}", exc_info=True)


def start_scheduler() -> AsyncIOScheduler:
    """Initialize and start background scheduler."""
    global scheduler
    if scheduler is not None and scheduler.running:
        return scheduler

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_periodic_sync,
        trigger=IntervalTrigger(minutes=settings.sync_interval_minutes),
        id="vrm_periodic_sync",
        name="VRM Periodic Synchronization",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"APScheduler started with {settings.sync_interval_minutes}m interval.")
    return scheduler


def shutdown_scheduler():
    """Gracefully shutdown background scheduler."""
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
        scheduler = None

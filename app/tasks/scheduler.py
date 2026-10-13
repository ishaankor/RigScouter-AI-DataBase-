import logging
from app.tasks.daily_price_refresh import run_daily_price_refresh_job
from app.services.digest_service import digest_service

logger = logging.getLogger("rigscouter.scheduler")

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAS_APSCHEDULER = True
    scheduler = AsyncIOScheduler()
except ImportError:
    HAS_APSCHEDULER = False
    scheduler = None

def start_scheduler():
    """
    Starts the authoritative unified in-app scheduler.
    Replaces fragmented GitHub Actions & Supabase crons.
    """
    if not HAS_APSCHEDULER:
        logger.warning("[Scheduler Notice] apscheduler not installed in local environment. Scheduled jobs will trigger via endpoints or once dependencies are installed.")
        return

    if scheduler and scheduler.running:
        return

    # 1. Daily Price Refresh at 00:00 UTC (12:00 AM UTC)
    scheduler.add_job(
        run_daily_price_refresh_job,
        trigger=CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="daily_price_refresh",
        replace_existing=True,
        max_instances=1
    )

    # 2. Daily Hardware Digest Dispatch at 08:00 UTC (8:00 AM UTC)
    scheduler.add_job(
        digest_service.dispatch_all_subscribers,
        trigger=CronTrigger(hour=8, minute=0, timezone="UTC"),
        id="daily_digest_dispatch",
        replace_existing=True,
        max_instances=1
    )

    scheduler.start()
    logger.info("⏰ [Unified In-App Scheduler] Started: (00:00 UTC Price Refresh, 08:00 UTC Digest Dispatch)")

def stop_scheduler():
    if HAS_APSCHEDULER and scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("[Unified In-App Scheduler] Stopped.")

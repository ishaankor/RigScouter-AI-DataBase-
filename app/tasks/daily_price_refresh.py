import asyncio
import logging
from datetime import datetime, timezone, timedelta
from app.core.database import supabase
from app.services.scraper_service import scraper_service
from app.services.pricing_service import pricing_service

logger = logging.getLogger("rigscouter.tasks.refresh")

async def prune_old_price_snapshots(retention_days: int = 90):
    """
    Deletes sub-hourly price snapshots older than `retention_days` to prevent
    unbounded table bloat, maintaining peak database query performance.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    try:
        res = await asyncio.to_thread(
            supabase.table("price_snapshots")
            .delete()
            .lt("scraped_at", cutoff)
            .execute
        )
        logger.info(f"🧹 [Snapshot Pruning] Cleared old snapshots prior to {cutoff}")
    except Exception as e:
        logger.debug(f"[Snapshot Pruning Notice] {e}")

async def run_daily_price_refresh_job():
    """
    Iterates through all hardware components in catalog,
    fetches live retailer prices, updates database, and broadcasts SSE.
    """
    logger.info("🌟 [Daily Price Refresh Job Started]")
    try:
        res = await asyncio.to_thread(
            supabase.table("hardware_components").select("*").execute
        )
        components = res.data or []
        logger.info(f"📊 [Daily Refresh] Found {len(components)} components to refresh.")

        for comp in components:
            comp_id = comp.get("id")
            name = comp.get("name") or comp.get("model") or comp_id
            product_url = comp.get("product_url")

            offers = []
            if product_url and product_url.startswith("http"):
                off = await scraper_service.get_price_for_url(product_url)
                if off:
                    offers.append(off)

            if not offers:
                search_q = comp.get("model") or name
                # Force live scrape during scheduled daily refresh
                offers = await scraper_service.find_all_offers(
                    query=search_q,
                    comp_id=comp_id,
                    force=True
                )

            if offers:
                await pricing_service.update_component_and_watchlist(comp_id, name, offers)

            await asyncio.sleep(1.0)  # Gentle spacing to avoid hitting rate limits

        # Run maintenance cleanup at the end of refresh
        await prune_old_price_snapshots(retention_days=90)

        logger.info("✅ [Daily Price Refresh Job Completed]")
    except Exception as e:
        logger.error(f"❌ [Daily Price Refresh Job Failed] {e}")

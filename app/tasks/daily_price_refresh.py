import asyncio
import logging
from datetime import datetime, timezone
from app.core.database import supabase
from app.services.scraper_service import scraper_service
from app.services.pricing_service import pricing_service

logger = logging.getLogger("rigscouter.tasks.refresh")

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
                offers = await scraper_service.find_all_offers(search_q)

            if offers:
                await pricing_service.update_component_and_watchlist(comp_id, name, offers)

            await asyncio.sleep(1.0)  # Gentle spacing to avoid hitting rate limits

        logger.info("✅ [Daily Price Refresh Job Completed]")
    except Exception as e:
        logger.error(f"❌ [Daily Price Refresh Job Failed] {e}")

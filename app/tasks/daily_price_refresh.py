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

        # Merge watchlist_items so every user's tracked item is guaranteed to be updated
        try:
            wl_res = await asyncio.to_thread(supabase.table("watchlist_items").select("*").execute)
            wl_items = wl_res.data or []
            existing_ids = {c.get("id") for c in components if c.get("id")}
            for wl in wl_items:
                wl_id = wl.get("component_id")
                if wl_id and wl_id not in existing_ids:
                    components.append({
                        "id": wl_id,
                        "name": wl.get("component_name"),
                        "model": wl.get("component_name"),
                        "product_url": wl.get("product_url"),
                        "current_price": wl.get("current_price"),
                        "lowest_price_90d": wl.get("all_time_low"),
                        "retailer": wl.get("retailer") or "Amazon",
                        "specs": "{}"
                    })
                    existing_ids.add(wl_id)
        except Exception as wl_ex:
            logger.debug(f"[Daily Refresh Watchlist Fetch Notice] {wl_ex}")

        logger.info(f"📊 [Daily Refresh] Found {len(components)} components to refresh.")

        import json

        for comp in components:
            comp_id = comp.get("id")
            name = comp.get("name") or comp.get("model") or comp_id
            product_url = comp.get("product_url")

            raw_specs = comp.get("specs") or {}
            specs_data = json.loads(raw_specs) if isinstance(raw_specs, str) else raw_specs

            offers = []
            seen_retailers = set()

            # 1. Primary offer from product_url (Amazon or eBay)
            if product_url and product_url.startswith("http"):
                try:
                    off = await scraper_service.get_price_for_url(product_url)
                    if off:
                        offers.append(off)
                        seen_retailers.add(off.retailer.lower().strip())
                except Exception as ex:
                    logger.warning(f"Error fetching primary URL {product_url}: {ex}")

            # 2. Check other retailer URLs from existing specs.RetailerOffers (strictly Amazon & eBay)
            existing_offers = specs_data.get("RetailerOffers") or []
            for ret_off in existing_offers:
                r_name = (ret_off.get("retailer") or "").lower().strip()
                r_url = ret_off.get("url")
                if r_url and r_url.startswith("http") and r_name in ["amazon", "ebay"] and r_name not in seen_retailers:
                    try:
                        scraped = await scraper_service.get_price_for_url(r_url)
                        if scraped:
                            offers.append(scraped)
                            seen_retailers.add(r_name)
                    except Exception as ex:
                        logger.debug(f"Error checking secondary retailer {r_name}: {ex}")

            # 3. Fallback to broad search if no direct URLs yielded offers
            if not offers:
                search_q = comp.get("model") or name
                offers = await scraper_service.find_all_offers(
                    query=search_q,
                    comp_id=comp_id,
                    force=True
                )

            # 4. Always update component (even if empty, to trigger miss tracking & grace period)
            await pricing_service.update_component_and_watchlist(comp_id, name, offers)

            await asyncio.sleep(1.0)  # Gentle spacing to avoid hitting rate limits

        # Run maintenance cleanup at the end of refresh
        await prune_old_price_snapshots(retention_days=90)

        logger.info("✅ [Daily Price Refresh Job Completed]")
    except Exception as e:
        logger.error(f"❌ [Daily Price Refresh Job Failed] {e}")

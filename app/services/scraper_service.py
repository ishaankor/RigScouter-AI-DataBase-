import re
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Tuple
from app.models.offer import Offer, ProductAnalysis
from app.core.database import supabase
from app.scrapers import (
    AmazonScraper,
    BestBuyScraper,
    NeweggScraper,
    MicroCenterScraper,
    EbayScraper,
)

logger = logging.getLogger("rigscouter.services.scraper")

class ScraperService:
    def __init__(self, max_concurrent: int = 4):
        self.scrapers = {
            "amazon": AmazonScraper(),
            "ebay": EbayScraper(),
        }
        # Concurrency semaphore prevents flooding outbound networks & retailer anti-bot systems
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Fast in-memory cache to collapse simultaneous requests (query -> (timestamp, offers))
        self._in_memory_cache: Dict[str, Tuple[float, List[Offer]]] = {}

    def _normalize_key(self, text: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")

    async def get_cached_offers(
        self,
        query: str,
        comp_id: Optional[str] = None,
        ttl_hours: float = 4.0
    ) -> Optional[List[Offer]]:
        """
        Checks 2-tier cache (In-memory + Supabase hardware_components).
        Returns cached Offer objects if fresh (< ttl_hours), avoiding live scraping.
        """
        now_ts = time.time()
        norm_q = self._normalize_key(query)

        # Tier 1: Check in-memory cache (instant)
        if norm_q in self._in_memory_cache:
            cached_time, cached_offers = self._in_memory_cache[norm_q]
            if (now_ts - cached_time) < (ttl_hours * 3600.0) and cached_offers:
                logger.info(f"⚡ [Tier-1 In-Memory Cache Hit] '{query}' ({len(cached_offers)} offers, age: {(now_ts - cached_time)/60:.1f}m)")
                return cached_offers

        # Tier 2: Check Supabase hardware_components table
        target_id = comp_id or norm_q
        try:
            res = await asyncio.to_thread(
                supabase.table("hardware_components")
                .select("id, name, current_price, updated_at, specs")
                .or_(f"id.eq.{target_id},id.ilike.%{target_id}%")
                .limit(1)
                .execute
            )
            if res.data:
                row = res.data[0]
                updated_at_str = row.get("updated_at")
                if updated_at_str:
                    updated_dt = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                    age_hours = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 3600.0

                    if age_hours < ttl_hours:
                        specs_raw = row.get("specs") or {}
                        specs_dict = json.loads(specs_raw) if isinstance(specs_raw, str) else specs_raw
                        retailer_offers = specs_dict.get("RetailerOffers") or []

                        if retailer_offers:
                            parsed_offers: List[Offer] = []
                            for ro in retailer_offers:
                                try:
                                    parsed_offers.append(Offer(
                                        retailer=ro.get("retailer", "Store"),
                                        price=float(ro.get("price") or 0.0),
                                        original_price=float(ro.get("originalPrice")) if ro.get("originalPrice") else None,
                                        url=ro.get("url", "#"),
                                        image_url=ro.get("imageUrl"),
                                        in_stock=ro.get("inStock", True),
                                        title=ro.get("title")
                                    ))
                                except Exception:
                                    continue

                            if parsed_offers:
                                # Populate tier 1 cache
                                self._in_memory_cache[norm_q] = (now_ts, parsed_offers)
                                logger.info(f"⚡ [Tier-2 Database Cache Hit] '{query}' (age: {age_hours:.1f}h < TTL: {ttl_hours}h)")
                                return parsed_offers
        except Exception as e:
            logger.debug(f"[Cache Lookup Notice] {e}")

        return None

    async def find_all_offers(
        self,
        query: str,
        comp_id: Optional[str] = None,
        analysis: Optional[ProductAnalysis] = None,
        retailers: Optional[List[str]] = None,
        timeout: float = 12.0,
        force: bool = False,
        ttl_hours: float = 4.0
    ) -> List[Offer]:
        """
        Queries selected retailers concurrently with:
        1. 4-hour cache deduplication (unless force=True)
        2. Concurrency semaphore guarding outbound traffic
        3. Timeout isolation per retailer adapter
        """
        # Step 1: Check cache unless explicit force refresh
        if not force:
            cached = await self.get_cached_offers(query, comp_id=comp_id, ttl_hours=ttl_hours)
            if cached:
                return cached

        # Step 2: Live scrape under concurrency semaphore
        async with self._semaphore:
            logger.info(f"🌐 [Live Scrape Initiated] Query: '{query}' (Workers active)")
            targets = [r.lower().replace(" ", "").replace("-", "") for r in (retailers or list(self.scrapers.keys()))]
            active_scrapers = []

            from agent import ProductAnalyzer
            effective_analysis = analysis
            if not effective_analysis and query:
                effective_analysis = ProductAnalysis(query, None, query, "Hardware", True, query, [])
            is_legacy = ProductAnalyzer.is_legacy_or_discontinued(effective_analysis) if effective_analysis else False

            for key, scraper in self.scrapers.items():
                if is_legacy and key in ["microcenter", "bestbuy"]:
                    logger.info(f"📦 [Hardware Lifecycle] Skipping {scraper.retailer_name} for legacy/discontinued hardware: '{query}'")
                    continue
                if any(t in key or key in t for t in targets):
                    active_scrapers.append(scraper)

            if not active_scrapers:
                active_scrapers = [s for k, s in self.scrapers.items() if not (is_legacy and k in ["microcenter", "bestbuy"])]

            async def _safe_search(scraper):
                try:
                    return await asyncio.wait_for(scraper.search(query, analysis), timeout=timeout)
                except Exception as e:
                    logger.warning(f"[{scraper.retailer_name} Search Timeout/Error] {e}")
                    return []

            results = await asyncio.gather(*[_safe_search(s) for s in active_scrapers])
            all_offers: List[Offer] = []
            for r in results:
                all_offers.extend(r)

            # Sort by price ascending
            all_offers.sort(key=lambda o: o.price)

            # Populate in-memory cache
            if all_offers:
                norm_q = self._normalize_key(query)
                self._in_memory_cache[norm_q] = (time.time(), all_offers)

            return all_offers

    async def get_price_for_url(self, url: str) -> Optional[Offer]:
        """Direct price lookup router based on URL domain guarded by semaphore."""
        async with self._semaphore:
            u_lower = url.lower()
            if "amazon." in u_lower or "amzn.to" in u_lower:
                return await self.scrapers["amazon"].get_price(url)
            elif "ebay." in u_lower:
                return await self.scrapers["ebay"].get_price(url)
            return None

scraper_service = ScraperService()

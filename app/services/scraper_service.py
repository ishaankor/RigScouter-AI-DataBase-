import asyncio
import logging
from typing import List, Optional, Dict
from app.models.offer import Offer, ProductAnalysis, PriceCheckResponse
from app.scrapers import (
    AmazonScraper,
    BestBuyScraper,
    NeweggScraper,
    MicroCenterScraper,
    EbayScraper,
)

logger = logging.getLogger("rigscouter.services.scraper")

class ScraperService:
    def __init__(self):
        self.scrapers = {
            "amazon": AmazonScraper(),
            "bestbuy": BestBuyScraper(),
            "newegg": NeweggScraper(),
            "microcenter": MicroCenterScraper(),
            "ebay": EbayScraper(),
        }

    async def find_all_offers(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None,
        retailers: Optional[List[str]] = None,
        timeout: float = 12.0
    ) -> List[Offer]:
        """
        Queries selected retailers concurrently with timeout isolation.
        """
        targets = [r.lower().replace(" ", "").replace("-", "") for r in (retailers or list(self.scrapers.keys()))]
        active_scrapers = []

        for key, scraper in self.scrapers.items():
            if any(t in key or key in t for t in targets):
                active_scrapers.append(scraper)

        if not active_scrapers:
            active_scrapers = list(self.scrapers.values())

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
        return all_offers

    async def get_price_for_url(self, url: str) -> Optional[Offer]:
        """Direct price lookup router based on URL domain."""
        u_lower = url.lower()
        if "amazon.com" in u_lower:
            return await self.scrapers["amazon"].get_price(url)
        elif "bestbuy.com" in u_lower:
            return await self.scrapers["bestbuy"].get_price(url)
        elif "newegg.com" in u_lower:
            return await self.scrapers["newegg"].get_price(url)
        elif "microcenter.com" in u_lower:
            return await self.scrapers["microcenter"].get_price(url)
        elif "ebay.com" in u_lower:
            return await self.scrapers["ebay"].get_price(url)
        return None

scraper_service = ScraperService()

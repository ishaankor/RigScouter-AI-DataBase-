import re
import logging
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search
from app.scrapers.smart import parse_result_text

logger = logging.getLogger("rigscouter.scrapers.microcenter")

class MicroCenterScraper(BaseScraper):
    retailer_name = "Micro Center"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.microcenter.com/",
    }

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()

        # Query Micro Center via Tavily targeting product pages
        t_query = f'site:microcenter.com/product/ "{clean_query}" price'
        results = await query_tavily_search(t_query, max_results=4)

        cat = getattr(analysis, "category", None) if analysis else None

        for item in results:
            url = item.get("url", "")
            if "microcenter.com/product/" not in url:
                continue

            raw_title = item.get("title", "")
            snippet = item.get("content", "")

            # Smart LLM-powered extraction
            smart_listings = await parse_result_text(
                f"{raw_title}\n{snippet}",
                self.retailer_name,
                url,
                clean_query,
                cat
            )
            for sl in smart_listings:
                is_valid, _ = validate_offer_fast(sl["title"], sl["price"], analysis)
                if is_valid:
                    offers.append(Offer(
                        retailer=self.retailer_name,
                        price=sl["price"],
                        original_price=sl.get("original_price"),
                        url=url,
                        title=sl["title"],
                        snippet=snippet,
                        in_stock=sl.get("in_stock", True),
                        condition=sl.get("condition", "new")
                    ))

        if offers:
            # Sort to prioritize in-stock offers first, then best price
            offers.sort(key=lambda x: (not x.in_stock, x.price))

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        t_hits = await query_tavily_search(f'"{url}" price', max_results=2)
        if t_hits:
            hit = t_hits[0]
            from app.scrapers.smart import parse_result_text
            txt = (hit.get("title", "") or "") + "\n" + (hit.get("content", "") or "")
            smart_listings = await parse_result_text(txt, self.retailer_name, url, url)
            if smart_listings:
                sl = smart_listings[0]
                return Offer(
                    retailer=self.retailer_name,
                    price=sl["price"],
                    original_price=sl.get("original_price"),
                    url=url,
                    title=sl.get("title") or hit.get("title"),
                    in_stock=sl.get("in_stock", True),
                    condition=sl.get("condition", "new")
                )
        return None

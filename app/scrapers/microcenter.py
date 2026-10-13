import re
import logging
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search, parse_tavily_snippet_prices

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

        for item in results:
            url = item.get("url", "")
            if "microcenter.com/product/" not in url:
                continue

            raw_title = item.get("title", "")
            snippet = item.get("content", "")
            price, orig = parse_tavily_snippet_prices(snippet + " " + raw_title)

            # Detect out of stock indicators in snippet and title
            txt_lower = f"{raw_title} {snippet}".lower()
            is_sold_out = any(
                phrase in txt_lower for phrase in [
                    "0 in stock", "sold out", "out of stock", "currently unavailable",
                    "not in stock", "backorder", "pick up in store only - 0",
                    "0 available", "not available online", "discontinued"
                ]
            )
            in_stock = not is_sold_out

            if price and price > 0:
                is_valid, _ = validate_offer_fast(raw_title, price, analysis)
                if is_valid:
                    offers.append(Offer(
                        retailer=self.retailer_name,
                        price=price,
                        original_price=orig,
                        url=url,
                        title=raw_title,
                        snippet=snippet,
                        in_stock=in_stock
                    ))

        if offers:
            # Sort to prioritize in-stock offers first, then best price
            offers.sort(key=lambda x: (not x.in_stock, x.price))

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        t_hits = await query_tavily_search(f'"{url}" price', max_results=2)
        if t_hits:
            hit = t_hits[0]
            p, orig = parse_tavily_snippet_prices(hit.get("content", "") + " " + hit.get("title", ""))
            if p:
                return Offer(
                    retailer=self.retailer_name,
                    price=p,
                    original_price=orig,
                    url=url,
                    title=hit.get("title")
                )
        return None

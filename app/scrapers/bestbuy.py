import re
import logging
from typing import List, Optional
import httpx
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search, parse_tavily_snippet_prices

logger = logging.getLogger("rigscouter.scrapers.bestbuy")

class BestBuyScraper(BaseScraper):
    retailer_name = "Best Buy"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bestbuy.com/",
    }

    async def _fetch_priceblocks(self, sku: str) -> Optional[dict]:
        """Queries Best Buy's direct priceBlocks API."""
        url = f"https://www.bestbuy.com/api/3.0/priceBlocks?skus={sku}"
        try:
            async with httpx.AsyncClient(headers=self.HEADERS, timeout=8.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list) and data:
                        return data[0]
        except Exception as e:
            logger.debug(f"[Best Buy priceBlocks API error] {e}")
        return None

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()

        # Query Best Buy via Tavily targeting product pages
        t_query = f'site:bestbuy.com/site "{clean_query}" price'
        results = await query_tavily_search(t_query, max_results=5)

        for item in results:
            url = item.get("url", "")
            if "bestbuy.com/site/" not in url or "/reviews" in url or "/questions" in url:
                continue

            raw_title = item.get("title", "")
            snippet = item.get("content", "")

            # Check for SKU in URL to try direct priceBlocks API for high precision
            sku_match = re.search(r"/([0-9]{7,8})\.p", url)
            sku = sku_match.group(1) if sku_match else None

            price = None
            orig_price = None

            if sku:
                pb = await self._fetch_priceblocks(sku)
                if pb:
                    price = pb.get("customerPrice")
                    orig_price = pb.get("regularPrice")

            if not price:
                price, orig_price = parse_tavily_snippet_prices(snippet + " " + raw_title)

            if price:
                is_valid, _ = validate_offer_fast(raw_title, price, analysis)
                if is_valid:
                    offers.append(Offer(
                        retailer=self.retailer_name,
                        price=price,
                        original_price=orig_price,
                        url=url,
                        title=raw_title,
                        snippet=snippet,
                        in_stock=True
                    ))
                    if len(offers) >= 2:
                        break

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        sku_match = re.search(r"/([0-9]{7,8})\.p", url)
        sku = sku_match.group(1) if sku_match else None

        if sku:
            pb = await self._fetch_priceblocks(sku)
            if pb and pb.get("customerPrice"):
                return Offer(
                    retailer=self.retailer_name,
                    price=float(pb["customerPrice"]),
                    original_price=float(pb.get("regularPrice", 0)) or None,
                    url=url,
                    title="Best Buy Product"
                )

        # Fallback to Tavily
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

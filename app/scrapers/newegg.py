import re
import logging
from typing import List, Optional
import httpx
from bs4 import BeautifulSoup
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search, parse_tavily_snippet_prices

logger = logging.getLogger("rigscouter.scrapers.newegg")

class NeweggScraper(BaseScraper):
    retailer_name = "Newegg"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.newegg.com/",
    }

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()
        search_url = f"https://www.newegg.com/p/pl?d={httpx.URL('', params={'d': clean_query}).query.decode('utf-8')}"

        try:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=10.0) as client:
                res = await client.get(search_url)
                if res.status_code == 200 and "are you a human" not in res.text.lower():
                    soup = BeautifulSoup(res.text, "html.parser")
                    items = soup.find_all("div", class_="item-cell")
                    for it in items[:4]:
                        title_el = it.find("a", class_="item-title")
                        title = title_el.text.strip() if title_el else ""
                        item_url = title_el.get("href") if title_el else ""

                        price_current = it.find("li", class_="price-current")
                        if not price_current:
                            continue

                        strong = price_current.find("strong")
                        sup = price_current.find("sup")
                        if not strong:
                            continue

                        val_str = f"{strong.text.replace(',', '').strip()}{sup.text.strip() if sup else '.00'}"
                        try:
                            price = float(val_str)
                        except ValueError:
                            continue

                        is_valid, _ = validate_offer_fast(title, price, analysis)
                        if is_valid:
                            offers.append(Offer(
                                retailer=self.retailer_name,
                                price=price,
                                url=item_url or search_url,
                                title=title,
                                in_stock=True
                            ))
        except Exception as e:
            logger.debug(f"[Newegg Direct Scrape Notice] {e}")

        # Fallback to Tavily if direct request was blocked or empty
        if not offers:
            t_query = f'site:newegg.com/p/ "{clean_query}" price'
            results = await query_tavily_search(t_query, max_results=4)
            for item in results:
                url = item.get("url", "")
                if "newegg.com/p/" not in url:
                    continue

                raw_title = item.get("title", "")
                snippet = item.get("content", "")
                price, orig = parse_tavily_snippet_prices(snippet + " " + raw_title)

                if price:
                    is_valid, _ = validate_offer_fast(raw_title, price, analysis)
                    if is_valid:
                        offers.append(Offer(
                            retailer=self.retailer_name,
                            price=price,
                            original_price=orig,
                            url=url,
                            title=raw_title,
                            snippet=snippet,
                            in_stock=True
                        ))
                        break

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

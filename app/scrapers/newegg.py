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

logger = logging.getLogger("rigscouter.scrapers.newegg")

class NeweggScraper(BaseScraper):
    retailer_name = "Newegg"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.newegg.com/",
    }

    @staticmethod
    def is_product_url(url: str) -> bool:
        """
        Validates that a URL points to an actual Newegg product page.
        Strictly rejects search listing pages (/p/pl, pl?d=), category hubs, and review pages.
        """
        if not url or "newegg.com" not in url:
            return False
        u_lower = url.lower()

        # Reject search query listings, brand pages, reviews, and community hubs
        search_indicators = [
            "/p/pl", "pl?d=", "/pl?", "/search/", "submit=", "submittype=",
            "/reviews", "/community", "/promotions/", "/todays-deals"
        ]
        if any(ind in u_lower for ind in search_indicators):
            return False

        # Must have canonical Newegg product pattern: /p/<ItemId> (not pl) or product.aspx?item=
        m = re.search(r'/p/([a-z0-9_-]+)', u_lower)
        if m and m.group(1) != "pl":
            return True

        if "product.aspx?item=" in u_lower:
            return True

        return False

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()
        search_url = f"https://www.newegg.com/p/pl?d={httpx.URL('', params={'d': clean_query}).query.decode('utf-8')}"

        # 1. Attempt Direct Search Scraping
        try:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=10.0) as client:
                res = await client.get(search_url)
                if res.status_code == 200 and "are you a human" not in res.text.lower():
                    soup = BeautifulSoup(res.text, "html.parser")
                    items = soup.find_all("div", class_="item-cell")
                    for it in items[:6]:
                        title_el = it.find("a", class_="item-title")
                        title = title_el.text.strip() if title_el else ""
                        raw_href = title_el.get("href", "").strip() if title_el else ""

                        # Normalize relative product URLs
                        if raw_href.startswith("//"):
                            item_url = "https:" + raw_href
                        elif raw_href.startswith("/"):
                            item_url = "https://www.newegg.com" + raw_href
                        else:
                            item_url = raw_href

                        # Strictly reject if not an actual product link (never fall back to search_url)
                        if not self.is_product_url(item_url):
                            continue

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
                                url=item_url,
                                title=title,
                                in_stock=True
                            ))
        except Exception as e:
            logger.debug(f"[Newegg Direct Scrape Notice] {e}")

        # 2. Targeted Product Page Search Fallback (excluding /p/pl landing pages)
        if not offers:
            # Explicitly exclude /p/pl from search queries so Tavily targets direct /p/N82E... product pages
            t_query = f'site:newegg.com/p/ -inurl:pl "{clean_query}" price'
            results = await query_tavily_search(t_query, max_results=6)
            if not results:
                # Fallback search without inurl filter, relying on is_product_url validator
                results = await query_tavily_search(f'site:newegg.com/p/ "{clean_query}" price', max_results=6)

            for item in results:
                url = item.get("url", "")
                if not self.is_product_url(url):
                    continue

                raw_title = item.get("title", "")
                snippet = item.get("content", "")
                cat = getattr(analysis, "category", None) if analysis else None

                # Smart LLM-powered extraction & validation
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

        # Sort to get best prices
        if offers:
            offers.sort(key=lambda x: (not x.in_stock, x.price))

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        if not self.is_product_url(url):
            return None
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

import re
import logging
from typing import List, Optional
from bs4 import BeautifulSoup
import httpx
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search, parse_tavily_snippet_prices

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger("rigscouter.scrapers.amazon")

class AmazonScraper(BaseScraper):
    retailer_name = "Amazon"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.amazon.com/",
    }

    async def _fetch_html(self, url: str) -> tuple[int, str]:
        status_code = 0
        text = ""

        if HAS_CURL_CFFI:
            for profile in ["chrome124", "chrome120"]:
                try:
                    async with CurlAsyncSession(impersonate=profile) as session:
                        res = await session.get(url, headers=self.HEADERS, timeout=12)
                        if res.status_code == 200 and "Robot Check" not in res.text:
                            return res.status_code, res.text
                except Exception:
                    pass

        try:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(url)
                return res.status_code, res.text
        except Exception as e:
            logger.debug(f"[Amazon Direct Fetch Notice] {e}")
            return status_code, text

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()
        search_url = f"https://www.amazon.com/s?k={httpx.URL('', params={'k': clean_query}).query.decode('utf-8')}"

        status_code, html = await self._fetch_html(search_url)

        if status_code == 200 and "Robot Check" not in html and html:
            soup = BeautifulSoup(html, "html.parser")
            items = soup.find_all("div", {"data-component-type": "s-search-result"})

            for it in items[:6]:
                try:
                    title_el = it.find("h2")
                    title = title_el.text.strip() if title_el else ""

                    price_whole = it.find("span", class_="a-price-whole")
                    price_fraction = it.find("span", class_="a-price-fraction")
                    if not price_whole:
                        continue

                    price_str = price_whole.text.replace(",", "").replace(".", "").strip()
                    frac_str = price_fraction.text.strip() if price_fraction else "00"
                    price = float(f"{price_str}.{frac_str}")

                    asin = it.get("data-asin", "")
                    url = f"https://www.amazon.com/dp/{asin}" if asin else search_url
                    img_el = it.find("img", class_="s-image")
                    img_url = img_el.get("src") if img_el else None

                    is_valid, _ = validate_offer_fast(title, price, analysis)
                    if is_valid:
                        offers.append(Offer(
                            retailer=self.retailer_name,
                            price=price,
                            url=url,
                            image_url=img_url,
                            title=title,
                            in_stock=True
                        ))
                except Exception:
                    continue

        # If direct search was blocked or returned no items, use smart Tavily search
        if not offers:
            logger.info(f"🔍 [Amazon Scraper] Using smart Tavily search for query: '{clean_query}'")
            tavily_hits = await query_tavily_search(f'site:amazon.com "{clean_query}" price', max_results=4)
            from app.scrapers.smart import parse_result_text
            for hit in tavily_hits:
                url = hit.get("url", "")
                if "amazon.com/dp/" not in url and "/gp/product/" not in url:
                    continue

                raw_title = hit.get("title", "")
                snippet = hit.get("content", "")
                txt = (raw_title or "") + "\n" + (snippet or "")

                smart_listings = await parse_result_text(
                    txt,
                    self.retailer_name,
                    url,
                    clean_query,
                    getattr(analysis, "category", None)
                )
                if not smart_listings:
                    continue

                sl = smart_listings[0]
                current_p = sl.get("price")
                orig_p = sl.get("original_price")
                parsed_title = sl.get("title") or raw_title
                in_stock = sl.get("in_stock", True)
                cond = sl.get("condition", "new")

                if current_p and current_p > 0:
                    is_valid, _ = validate_offer_fast(parsed_title, current_p, analysis)
                    if is_valid:
                        offers.append(Offer(
                            retailer=self.retailer_name,
                            price=current_p,
                            original_price=orig_p,
                            url=url,
                            title=parsed_title,
                            snippet=snippet,
                            in_stock=in_stock,
                            condition=cond
                        ))
                        break

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
        asin = asin_match.group(1) if asin_match else None
        target_url = f"https://www.amazon.com/dp/{asin}" if asin else url

        status_code, html = await self._fetch_html(target_url)
        if status_code == 200 and "Robot Check" not in html and html:
            soup = BeautifulSoup(html, "html.parser")
            price_el = soup.find("span", class_="a-price")
            if price_el:
                off = price_el.find("span", class_="a-offscreen")
                if off and off.text:
                    m = re.search(r"[\$]?([0-9,]+\.[0-9]{2})", off.text)
                    if m:
                        p = float(m.group(1).replace(",", ""))
                        title_el = soup.find("span", id="productTitle")
                        title = title_el.text.strip() if title_el else "Amazon Product"
                        return Offer(retailer=self.retailer_name, price=p, url=target_url, title=title)

        # Smart fallback for exact URL lookup
        if asin:
            t_hits = await query_tavily_search(f"site:amazon.com/dp/{asin} price", max_results=2)
            if t_hits:
                hit = t_hits[0]
                from app.scrapers.smart import parse_result_text
                txt = (hit.get("title", "") or "") + "\n" + (hit.get("content", "") or "")
                smart_listings = await parse_result_text(txt, self.retailer_name, target_url, asin)
                if smart_listings:
                    sl = smart_listings[0]
                    return Offer(
                        retailer=self.retailer_name,
                        price=sl["price"],
                        original_price=sl.get("original_price"),
                        url=target_url,
                        title=sl.get("title") or hit.get("title"),
                        in_stock=sl.get("in_stock", True),
                        condition=sl.get("condition", "new")
                    )

        return None

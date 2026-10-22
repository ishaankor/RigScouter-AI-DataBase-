import re
import logging
from typing import List, Optional, Tuple
import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search, fetch_with_zenrows
from app.scrapers.smart import parse_result_text

logger = logging.getLogger("rigscouter.scrapers.microcenter")

class MicroCenterScraper(BaseScraper):
    retailer_name = "Micro Center"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    }

    SAFARI_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }

    async def _fetch_html(self, url: str) -> Tuple[int, str]:
        """Fetches product HTML directly using curl_cffi TLS impersonation, falling back to residential proxy."""
        status_code = 0
        text = ""

        # Cookies to set general store view (151 is Santa Clara / West, 029 is Online/Web)
        cookies = {"storeSelected": "029"}

        if HAS_CURL_CFFI:
            for profile in ["chrome124", "chrome120", "safari18_0"]:
                try:
                    headers = self.SAFARI_HEADERS if "safari" in profile else self.HEADERS
                    async with CurlAsyncSession(impersonate=profile) as session:
                        res = await session.get(url, headers=headers, cookies=cookies, timeout=12)
                        if res.status_code == 200 and "Access Denied" not in res.text and len(res.text) > 2000:
                            return res.status_code, res.text
                        status_code, text = res.status_code, res.text
                except Exception as e:
                    logger.debug(f"[Micro Center curl_cffi notice ({profile})] {e}")

        # Fallback to httpx
        if not text or status_code != 200:
            try:
                async with httpx.AsyncClient(headers=self.HEADERS, cookies=cookies, follow_redirects=True, timeout=12.0) as client:
                    res = await client.get(url)
                    if res.status_code == 200 and "Access Denied" not in res.text and len(res.text) > 2000:
                        return res.status_code, res.text
                    status_code, text = res.status_code, res.text
            except Exception as e:
                logger.debug(f"[Micro Center httpx notice] {e}")

        # If blocked by Akamai, route through ZenRows residential proxy
        if status_code != 200 or "Access Denied" in text or len(text) < 1000:
            logger.info(f"🛡️ [Micro Center Direct] Blocked ({status_code}); attempting ZenRows proxy for {url}...")
            zr_code, zr_text = await fetch_with_zenrows(url)
            if zr_code == 200 and "Access Denied" not in zr_text and len(zr_text) > 1000:
                return zr_code, zr_text

        return status_code, text

    def _parse_product_page(self, html: str, url: str) -> Optional[Offer]:
        """Parses Micro Center DOM directly, stripping out add-on accessories and carousels."""
        if not html or len(html.strip()) < 50:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # 1. CRITICAL: Remove add-on accessories, warranty upsells, and related product carousels
        # This prevents Compressed Air Duster $29.99, Ultra Duster $9.99, or Thermal Paste from being extracted!
        for noise in soup.select(
            "#addOns, .add-ons-wrapper, .accessory-container, #accessories, "
            ".related-items, #inline-similar, .carousel, .upsell, .protection-plan, "
            "div[id*='addOn'], div[class*='addOn']"
        ):
            noise.decompose()

        # 2. Extract product title
        title_el = (
            soup.find("h1", {"data-name": True})
            or soup.find("span", {"data-name": True})
            or soup.find("h1", class_=re.compile(r"product[-_]?title", re.I))
            or soup.find("h1")
        )
        title = ""
        if title_el:
            title = title_el.get("data-name") or title_el.text.strip()
        if not title and soup.title:
            title = soup.title.string.replace("Micro Center", "").strip()

        if not title:
            return None

        # 3. Extract primary product price
        price_val: Optional[float] = None

        # A: Check meta itemprop="price"
        meta_price = soup.find("meta", {"itemprop": "price"})
        if meta_price and meta_price.get("content"):
            try:
                val = float(meta_price["content"].replace(",", "").strip())
                if val > 5.0:
                    price_val = val
            except ValueError:
                pass

        # B: Check main pricing span: #pricing
        if not price_val:
            pricing_el = soup.find(id="pricing") or soup.find("span", id="pricing")
            if pricing_el:
                m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', pricing_el.text)
                if m:
                    try:
                        val = float(m.group(1).replace(",", ""))
                        if val > 5.0:
                            price_val = val
                    except ValueError:
                        pass

        # C: Check schema / JSON-LD in script tag
        if not price_val:
            for s in soup.find_all("script", type="application/ld+json"):
                if s.string and '"price"' in s.string:
                    m = re.search(r'"price":\s*"?([0-9,]+\.[0-9]{2})"?', s.string)
                    if m:
                        try:
                            val = float(m.group(1).replace(",", ""))
                            if val > 5.0:
                                price_val = val
                                break
                        except ValueError:
                            pass

        if not price_val:
            return None

        # 4. Extract original / was price
        original_price = None
        orig_el = soup.find("span", class_="original-price") or soup.find("strike")
        if orig_el:
            m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', orig_el.text)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    if val > price_val:
                        original_price = val
                except ValueError:
                    pass

        # 5. Extract stock availability
        page_text = soup.text.lower()
        is_sold_out = any(phrase in page_text for phrase in [
            "sold out", "0 in stock", "out of stock", "currently unavailable",
            "no longer available", "this item is no longer available",
            "item no longer available", "no longer carried", "discontinued",
            "not available for shipping and sold out", "pick up in store only - 0",
            "not available in store or online", "not available for shipping"
        ])

        # Check dedicated inventory status tags in Micro Center DOM
        inv_el = soup.select_one(".inventoryCnt, .inventory, .out-of-stock, .unavailable, .order-status")
        if inv_el:
            inv_text = inv_el.text.lower()
            if any(p in inv_text for p in [
                "no longer available", "sold out", "0 in stock", "out of stock",
                "no longer carried", "unavailable", "discontinued"
            ]):
                is_sold_out = True

        # 6. Extract image
        img_el = (
            soup.find("img", id="productImage")
            or soup.find("img", class_="product-image")
            or soup.find("img", {"itemprop": "image"})
        )
        img_url = img_el.get("src") if img_el else None

        t_lower = title.lower()
        is_open_box = any(w in t_lower for w in ["open box", "open-box", "refurbished"])
        cond = "open_box" if is_open_box else "new"

        return Offer(
            retailer=self.retailer_name,
            price=price_val,
            original_price=original_price,
            url=url,
            image_url=img_url,
            title=title,
            in_stock=not is_sold_out,
            condition=cond
        )

    async def get_price(self, url: str) -> Optional[Offer]:
        """Authoritatively gets price by fetching live page HTML, falling back to smart Tavily extraction if blocked."""
        # 1. Attempt direct live HTML fetch & DOM extraction
        status, html = await self._fetch_html(url)
        if status == 200 and html:
            offer = self._parse_product_page(html, url)
            if offer and offer.price:
                logger.info(f"✅ [Micro Center DOM Hit] ${offer.price:.2f} -> {offer.title[:60]}")
                return offer

        # 2. Fallback to Tavily snippet search if direct page fetch was completely blocked
        logger.info(f"🔍 [Micro Center Fallback] Direct fetch blocked; querying Tavily for {url}...")
        t_hits = await query_tavily_search(f'"{url}" price', max_results=2)
        if t_hits:
            hit = t_hits[0]
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

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        offers: List[Offer] = []
        clean_query = query.replace('"', '').strip()

        # Query Micro Center product pages via Tavily
        t_query = f'site:microcenter.com/product/ "{clean_query}" price'
        results = await query_tavily_search(t_query, max_results=4)

        cat = getattr(analysis, "category", None) if analysis else None

        for item in results:
            url = item.get("url", "")
            if "microcenter.com/product/" not in url:
                continue

            # First try direct live DOM extraction for this product URL to get the true price ($59.99)
            live_offer = await self.get_price(url)
            if live_offer:
                is_valid, _ = validate_offer_fast(live_offer.title, live_offer.price, analysis)
                if is_valid:
                    offers.append(live_offer)
                    continue

            # If live DOM fetch failed, fall back to smart LLM-powered snippet parsing
            raw_title = item.get("title", "")
            snippet = item.get("content", "")
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
            # Deduplicate by URL
            unique_offers = {}
            for o in offers:
                if o.url not in unique_offers:
                    unique_offers[o.url] = o
            offers = list(unique_offers.values())
            # Prioritize in-stock offers first, then best price
            offers.sort(key=lambda x: (not x.in_stock, x.price))

        return offers

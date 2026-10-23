import base64
import time
import logging
import re
from typing import List, Optional, Tuple
import httpx
from bs4 import BeautifulSoup

from app.core.config import settings
from app.models.offer import Offer, ProductAnalysis
from app.scrapers.base import BaseScraper
from app.scrapers.validator import validate_offer_fast
from app.scrapers.search_fallback import query_tavily_search
from app.scrapers.smart import parse_result_text

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger("rigscouter.scrapers.ebay")

class EbayScraper(BaseScraper):
    retailer_name = "eBay"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ebay.com/",
    }

    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    @staticmethod
    def extract_item_id(text: str) -> Optional[str]:
        if not text:
            return None
        # Support /itm/123456789012, /itm/slug/123456789012, ?item=123456789012, ?itemId=123456789012, or raw numeric ID
        m = re.search(r'/itm/(?:[^/?#]+/)?(\d{9,15})', text)
        if m:
            return m.group(1)
        m = re.search(r'[?&](?:item|itemId|id)=(\d{9,15})', text, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r'/p/(\d{9,15})', text)
        if m:
            return m.group(1)
        clean = text.strip()
        if clean.isdigit() and len(clean) in range(9, 16):
            return clean
        return None

    async def _fetch_html(self, url: str) -> Tuple[int, str]:
        status_code = 0
        text = ""

        if HAS_CURL_CFFI:
            for profile in ["chrome124", "chrome120"]:
                try:
                    async with CurlAsyncSession(impersonate=profile) as session:
                        res = await session.get(url, headers=self.HEADERS, timeout=12)
                        if res.status_code == 200 and res.text:
                            return res.status_code, res.text
                except Exception:
                    pass

        try:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(url)
                return res.status_code, res.text
        except Exception as e:
            logger.debug(f"[eBay Direct Fetch Notice] {e}")
            return status_code, text

    async def _get_access_token(self) -> Optional[str]:
        client_id = settings.EBAY_CLIENT_ID
        client_secret = settings.EBAY_CLIENT_SECRET
        if not client_id or not client_secret:
            return None

        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token

        credentials = f"{client_id}:{client_secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()
        oauth_url = "https://api.ebay.com/identity/v1/oauth2/token"

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    oauth_url,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": f"Basic {encoded_creds}"
                    },
                    data={
                        "grant_type": "client_credentials",
                        "scope": "https://api.ebay.com/oauth/api_scope"
                    },
                    timeout=10.0
                )
                if res.status_code == 200:
                    data = res.json()
                    self._access_token = data.get("access_token")
                    expires_in = data.get("expires_in", 7200)
                    self._token_expires_at = time.time() + expires_in
                    return self._access_token
        except Exception as e:
            logger.warning(f"[eBay OAuth Exception] {e}")
        return None

    async def search(
        self,
        query: str,
        analysis: Optional[ProductAnalysis] = None
    ) -> List[Offer]:
        clean_query = query.replace('"', '').strip()
        token = await self._get_access_token()

        if token:
            browse_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        browse_url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
                        },
                        params={
                            "q": clean_query,
                            "limit": "6",
                            "filter": "buyingOptions:{FIXED_PRICE}"
                        },
                        timeout=12.0
                    )
                    if res.status_code == 200:
                        items = res.json().get("itemSummaries", [])
                        offers: List[Offer] = []
                        for it in items:
                            title = it.get("title", "")
                            price_obj = it.get("price", {})
                            price_val = float(price_obj.get("value", 0)) if price_obj.get("value") else 0
                            if price_val <= 0:
                                continue

                            condition = it.get("condition", "New")
                            if any(w in condition.lower() for w in ["parts", "not working", "broken"]):
                                continue

                            is_valid, _ = validate_offer_fast(title, price_val, analysis)
                            if is_valid:
                                item_url = it.get("itemWebUrl") or f"https://www.ebay.com/itm/{it.get('itemId')}"
                                img = (it.get("image") or {}).get("imageUrl")
                                offers.append(Offer(
                                    retailer=self.retailer_name,
                                    price=price_val,
                                    url=item_url,
                                    image_url=img,
                                    title=title,
                                    condition=condition,
                                    in_stock=True
                                ))
                                if len(offers) >= 2:
                                    break
                        if offers:
                            return offers
            except Exception as e:
                logger.debug(f"[eBay Search Exception] {e}")

        # Fallback to Tavily if API returned 0 items or token is unavailable
        logger.info(f"🔍 [eBay Scraper] Using Tavily fallback search for: '{clean_query}'")
        tavily_hits = await query_tavily_search(f'site:ebay.com/itm "{clean_query}" price', max_results=4)
        offers: List[Offer] = []
        for hit in tavily_hits:
            url = hit.get("url", "")
            if "ebay.com/itm" not in url:
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
                    if len(offers) >= 2:
                        break
            if len(offers) >= 2:
                break

        return offers

    async def get_price(self, url: str) -> Optional[Offer]:
        """
        Enterprise 3-tier lookup for exact eBay URLs:
        1. Official eBay Browse API by Legacy ID
        2. Direct HTML extraction (detects Buy It Now, Add to cart, price, condition)
        3. Tavily snippet search with LLM parsing
        """
        item_id = self.extract_item_id(url)
        token = await self._get_access_token()

        # Tier 1: Official eBay Browse API
        if item_id and token:
            api_url = f"https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id?legacy_item_id={item_id}"
            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    res = await client.get(
                        api_url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
                        }
                    )
                    if res.status_code == 200:
                        data = res.json()
                        price_obj = data.get("price", {})
                        price_str = price_obj.get("value")
                        if price_str:
                            price_val = float(price_str)
                            title = data.get("title", "")
                            condition_raw = data.get("condition", "Used")
                            c_lower = condition_raw.lower()
                            cond = "refurbished" if "refurbished" in c_lower else ("used" if any(w in c_lower for w in ["used", "pre-owned"]) else ("open_box" if "open" in c_lower else "new"))
                            image_url = (data.get("image") or {}).get("imageUrl")
                            item_url = data.get("itemWebUrl") or url

                            avail = data.get("estimatedAvailabilities", [])
                            in_stock = True
                            if avail and avail[0].get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
                                in_stock = False

                            return Offer(
                                retailer=self.retailer_name,
                                price=price_val,
                                url=item_url,
                                image_url=image_url,
                                title=title,
                                condition=cond,
                                in_stock=in_stock
                            )
            except Exception as e:
                logger.debug(f"[eBay API direct lookup notice] {e}")

        # Tier 2: Direct HTML DOM Extraction
        status_code, html = await self._fetch_html(url)
        if status_code == 200 and html:
            try:
                soup = BeautifulSoup(html, "html.parser")

                # Extract title
                title = None
                title_el = soup.find("h1", class_=re.compile(r"x-item-title__mainTitle", re.I)) or soup.find("h1")
                if title_el:
                    title = title_el.get_text(strip=True)
                elif soup.title:
                    title = soup.title.string.replace("| eBay", "").strip()

                # Extract price
                price_val = None
                price_container = (
                    soup.find("div", class_=re.compile(r"x-price-primary", re.I)) or
                    soup.find("div", class_=re.compile(r"x-bin-price", re.I)) or
                    soup.find("span", itemprop="price")
                )
                if price_container:
                    raw_p = price_container.get_text(strip=True)
                    m = re.search(r"[\$US\s]*([0-9,]+\.[0-9]{2})", raw_p)
                    if m:
                        price_val = float(m.group(1).replace(",", ""))

                # Fallback price regex across main container if container not found
                if not price_val:
                    m_p = re.search(r"US\s*\$([0-9,]+\.[0-9]{2})", html)
                    if m_p:
                        price_val = float(m_p.group(1).replace(",", ""))

                # Extract stock status: check for active buy buttons
                html_lower = html.lower()
                has_bin_button = bool(
                    soup.find(id="binBtn_btn_1") or
                    soup.find(class_=re.compile(r"x-bin-action", re.I)) or
                    re.search(r'["\'>]\s*buy it now\s*<', html_lower)
                )
                has_atc_button = bool(
                    soup.find(id="atcBtn_btn_1") or
                    soup.find(class_=re.compile(r"x-atc-action", re.I)) or
                    re.search(r'["\'>]\s*add to cart\s*<', html_lower)
                )
                is_active = has_bin_button or has_atc_button

                is_explicit_ended = any(phrase in html_lower for phrase in [
                    "this listing was ended by the seller",
                    "this item is out of stock",
                    "ended:"
                ])

                in_stock = is_active and not is_explicit_ended

                # Condition
                cond = "new"
                cond_el = soup.find("div", class_=re.compile(r"x-item-condition-text", re.I))
                if cond_el:
                    c_txt = cond_el.get_text(strip=True).lower()
                    if "refurbished" in c_txt:
                        cond = "refurbished"
                    elif any(w in c_txt for w in ["used", "pre-owned"]):
                        cond = "used"
                    elif "open" in c_txt:
                        cond = "open_box"

                img_el = soup.find("div", class_=re.compile(r"ux-image-carousel-item", re.I))
                img_url = img_el.find("img")["src"] if (img_el and img_el.find("img")) else None

                if price_val and price_val > 0:
                    logger.info(f"✅ [eBay Direct HTML Hit] ${price_val:.2f} (in_stock={in_stock}) -> {title[:60] if title else url}")
                    return Offer(
                        retailer=self.retailer_name,
                        price=price_val,
                        url=url,
                        image_url=img_url,
                        title=title or "eBay Hardware Product",
                        condition=cond,
                        in_stock=in_stock
                    )
            except Exception as dom_err:
                logger.debug(f"[eBay DOM parse notice] {dom_err}")

        # Tier 3: Tavily snippet search with LLM parsing
        query_target = f"site:ebay.com/itm/{item_id} price" if item_id else f'"{url}" price'
        t_hits = await query_tavily_search(query_target, max_results=2)
        if t_hits:
            hit = t_hits[0]
            txt = (hit.get("title", "") or "") + "\n" + (hit.get("content", "") or "")
            smart_listings = await parse_result_text(txt, self.retailer_name, url, item_id or url)
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

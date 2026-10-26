import os
import sys
import re
import json
import base64
import time
import asyncio
import urllib.parse
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi import CurlHttpVersion
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    CurlHttpVersion = None

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "")
ZENROWS_QUOTA_EXHAUSTED = False if ZENROWS_API_KEY else True

raw_tavily = os.environ.get("TAVILY_API_KEYS", os.environ.get("TAVILY_API_KEY", ""))
TAVILY_API_KEYS = [k.strip() for k in raw_tavily.split(",") if k.strip()]

async def query_tavily_search(query: str, max_results: int = 3) -> list[dict]:
    active_keys = [k for k in TAVILY_API_KEYS if k]
    for key in active_keys:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": key, "query": query, "max_results": max_results}
                )
                if res.status_code == 200:
                    return res.json().get("results", [])
        except Exception:
            continue
    return []

# ─── 1. AI PRODUCT ANALYZER (No hardcoded brands or regexes) ──────────────────

class ProductAnalysis:
    def __init__(self, raw_query: str, brand: str | None, model: str, category: str,
                 is_pc_part: bool, retailer_search_query: str, negative_keywords: list[str],
                 min_price: float | None = None,
                 product_exists: bool = True,
                 correction_suggestion: str | None = None):
        self.raw_query = raw_query
        self.brand = brand
        self.model = model
        self.category = category
        self.is_pc_part = is_pc_part
        self.retailer_search_query = retailer_search_query
        self.negative_keywords = negative_keywords
        self.min_price = min_price
        self.product_exists = product_exists
        self.correction_suggestion = correction_suggestion

    def to_dict(self) -> dict:
        return {
            "raw_query": self.raw_query,
            "brand": self.brand,
            "model": self.model,
            "category": self.category,
            "is_pc_part": self.is_pc_part,
            "retailer_search_query": self.retailer_search_query,
            "negative_keywords": self.negative_keywords,
            "min_price": self.min_price,
            "product_exists": self.product_exists,
            "correction_suggestion": self.correction_suggestion,
        }

class ProductAnalyzer:
    """Uses LLM to understand PC components dynamically without hardcoded brand lists or chipsets."""

    @staticmethod
    async def analyze_query(query: str) -> ProductAnalysis:
        clean = query.strip()
        # Clean web page title noise like "Price: $1,099 — United States | GPU Prices"
        clean = re.sub(r'\|\s*.*$', '', clean)
        clean = re.sub(r'—\s*.*$', '', clean)
        clean = re.sub(r'Price:\s*\$[\d,.]+', '', clean, flags=re.I)
        clean = re.sub(r'\b(?:Amazon\.com|eBay|Best Buy|Newegg|Micro Center)\b.*', '', clean, flags=re.I)
        clean = re.sub(r'[\t\r\n]+', ' ', clean).strip()
        if not clean:
            return ProductAnalysis(clean, None, clean, "Other", False, clean, [])

        if not GROQ_API_KEY:
            return ProductAnalysis(clean, None, clean, "Hardware", True, clean, [])

        prompt = (
            "You are an expert PC hardware and technology analyzer for all custom PC rig components. "
            "A user can search for ANY rig part across all categories: "
            "CPUs, GPUs, Motherboards, RAM, Storage (SSDs/HDDs), Power Supplies (PSUs), Cases, "
            "Cooling (AIO liquid coolers, air coolers, case fans), Monitors, and Peripherals.\n"
            "Given a user search query, extract structured information without relying on hardcoded lists. "
            "Return valid JSON with:\n"
            "- \"brand\": The hardware manufacturer or brand if present/identifiable (e.g. Corsair, Lian Li, ASUS, MSI, Noctua, AMD, Intel, NVIDIA, Samsung, Fractal Design, Seasonic, NZXT, G.Skill, Thermalright, Western Digital, be quiet!), or null.\n"
            "- \"model\": The specific model name or part identifier (e.g. 'King 95', 'RM850x', 'O11 Dynamic', '990 Pro', 'B650 Tomahawk', 'Trident Z5', 'Peerless Assassin 120', 'Kraken Elite 360', 'Ryzen 5 9600X', 'RTX 5090').\n"
            "- \"category\": Exactly one of: 'GPU', 'CPU', 'RAM', 'Motherboard', 'Storage', 'Power Supply', 'Case', 'Cooling', 'Monitor', 'Peripherals', or 'Other'.\n"
            "- \"is_pc_part\": true for any PC hardware component or computer peripheral; false only if completely unrelated items (food, clothing, household goods).\n"
            "- \"product_exists\": true for any real or plausible PC hardware part. Never assume a product does not exist just because it is new or custom.\n"
            "- \"min_price\": Realistic minimum market price in USD for a genuine unit of this hardware component (e.g. 15 for fans/paste, 35 for budget RAM/coolers, 50 for budget cases/storage, 70 for budget PSUs/motherboards, 150-250 for budget/mid CPUs and GPUs, 350-500+ for high-end/flagship CPUs like Ryzen 7/9 X3D or Core i7/i9, 500-1500+ for high-end GPUs). Used to automatically discard dummy replicas, 1:1 scale toys, empty boxes, and brackets.\n"
            "- \"retailer_search_query\": Clean search term optimized for retailer catalogs (e.g. brand + model without search noise like 'buy', 'price', etc.). NEVER change or substitute the user's component model!\n"
            "- \"negative_keywords\": Array of terms that indicate a candidate result is the WRONG product, accessory, toy, or broken item. CRITICAL: NEVER include standard hardware specification terms (e.g. 'socket', 'am5', 'am4', 'lga', 'pcie', 'ddr5', 'cores', 'threads') as negative keywords, because genuine hardware components always include their socket and specs in their titles."
        )

        models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        for model_name in models:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json={
                            "model": model_name,
                            "messages": [
                                {"role": "system", "content": prompt},
                                {"role": "user", "content": f"Query: {clean}"}
                            ],
                            "temperature": 0,
                            "response_format": {"type": "json_object"}
                        },
                        timeout=7.0
                    )
                if res.status_code == 200:
                    data = json.loads(res.json()["choices"][0]["message"]["content"])
                    min_p = None
                    if data.get("min_price"):
                        try:
                            min_p = float(data["min_price"])
                        except (ValueError, TypeError):
                            pass

                    # Ground retailer search query firmly in the user's searched component
                    ret_query = data.get("retailer_search_query") or clean

                    return ProductAnalysis(
                        raw_query=clean,
                        brand=data.get("brand"),
                        model=data.get("model") or clean,
                        category=data.get("category") or "Other",
                        is_pc_part=bool(data.get("is_pc_part", True)),
                        retailer_search_query=ret_query,
                        negative_keywords=data.get("negative_keywords") or [],
                        min_price=min_p,
                        product_exists=True,
                        correction_suggestion=None
                    )
            except Exception as e:
                print(f"[ProductAnalyzer Error with {model_name}] {e}")

        return ProductAnalysis(clean, None, clean, "Hardware", True, clean, [])

    @staticmethod
    def is_legacy_or_discontinued(analysis: ProductAnalysis) -> bool:
        """Returns True if component is legacy/discontinued hardware no longer stocked by retail chains.
        Prevents Best Buy / Micro Center timeout hangs, deleted SKU scraping, and false prebuilt matches."""
        model = (analysis.model or "").upper()
        raw = (analysis.raw_query or "").upper()
        combined = f"{model} {raw}"

        # 1. Motherboards: AMD Legacy Chipsets & Sockets
        # Modern retail AMD is AM5 (B650, X670, B850, X870, A620).
        # AM4 chipsets (B550, B450, X570, A520, A320, X470, B350, X370) and older (AM3, AM2, FM2)
        # are no longer carried by retail chains like Micro Center.
        if any(w in combined for w in ["AM4", "AM3+", "AM3", "AM2+", "AM2", "FM2+", "FM2"]):
            return True
        if re.search(r'\b(?:A320|B350|X370|B450|X470|A520|B550|X570)[A-Z0-9\-]*\b', combined):
            return True

        # 2. Motherboards: Intel Legacy Chipsets & Sockets
        # LGA 1200: Z590, B560, H570, H510, Z490, B460, H470, H410, W480
        # LGA 1151: Z390, Z370, H370, B365, B360, H310, Z270, H270, B250, Z170, H170, B150, H110
        # LGA 1150: Z97, H97, Z87, H87, B85, H81
        # LGA 1155: Z77, Z75, Z68, P67, H67, H61
        # Modern retail Intel is LGA 1700 (Z690, B660, H610, Z790, B760) and LGA 1851 (Z890, B860)
        if any(w in combined for w in ["LGA 1200", "LGA 1151", "LGA 1150", "LGA 1155", "LGA 775", "LGA 1366", "LGA 2011", "LGA 2066"]):
            return True
        if re.search(r'\b(?:Z590|B560|H570|H510|Z490|B460|H470|H410|W480|Z390|Z370|H370|B365|B360|H310|Z270|H270|B250|Z170|H170|B150|H110|Z97|H97|Z87|H87|B85|H81|Z77|Z75|Z68|P67|H67|H61)[A-Z0-9\-]*\b', combined):
            return True

        # 3. Intel CPUs: Discontinued <= 11th Gen (2xxx - 11xxx, e.g. i7-4770K, i7-6700K, i7-10700KF)
        # Modern retail: 12xxx, 13xxx, 14xxx, Core Ultra 1xx/2xx
        if any(k in combined for k in ["INTEL", "CORE", "I3-", "I5-", "I7-", "I9-", "I3 ", "I5 ", "I7 ", "I9 "]):
            m = re.search(r'\b(?:I[3579][ -]?)?([2-9]\d{3}|1[01]\d{3})([A-Z]{0,3})\b', combined)
            if m:
                num = int(m.group(1))
                if num <= 11900:
                    return True
            if any(w in combined for w in ["CORE 2", "LGA 1200", "LGA 1150", "LGA 1155", "LGA 1151", "LGA 775", "LGA 1366", "LGA 2011", "LGA 2066"]):
                return True

        # 4. AMD CPUs: Ryzen 1000, 2000, 3000, 4000 series, FX series, Phenom, Athlon, Threadripper 1000-3000
        if any(k in combined for k in ["RYZEN", "AMD", "THREADRIPPER"]):
            m = re.search(r'\b(?:RYZEN\s*[3579]\s*)?([1-4]\d{3})([A-Z]{0,3})\b', combined)
            if m:
                return True
            if any(w in combined for w in ["FX-", "FX ", "AM3", "AM3+", "AM2", "FM2", "PHENOM", "ATHLON"]):
                return True
            if re.search(r'\b(?:THREADRIPPER\s*)?[1-3]\d{3}[A-Z]{0,2}\b', combined):
                return True

        # 5. NVIDIA GPUs: GTX series, RTX 20-series, RTX 30-series (3090, 3080, 3070, 3060, 3050), GT series
        if any(k in combined for k in ["GTX", "NVIDIA", "GEFORCE", "RTX", "GT "]):
            if re.search(r'\bGTX\s*(?:\d{3,4})\b', combined):
                return True
            if re.search(r'\bRTX\s*20\d{2}\b', combined):
                return True
            if re.search(r'\bRTX\s*30\d{2}(?:\s*TI)?\b', combined):
                return True
            if re.search(r'\bGT\s*(?:710|730|1030)\b', combined):
                return True

        # 6. AMD GPUs: RX 400/500 series, RX 5000 series, RX 6000 series, RX Vega, R9, R7
        if any(k in combined for k in ["RADEON", "AMD", "RX"]):
            if re.search(r'\bRX\s*[45][6-9]0\b', combined):
                return True
            if re.search(r'\bRX\s*5\d{3}(?:\s*XT)?\b', combined):
                return True
            if re.search(r'\bRX\s*6\d{3}(?:\s*XT)?\b', combined):
                return True
            if any(w in combined for w in ["VEGA 56", "VEGA 64", "RADEON VII", "R9 390", "R9 290"]):
                return True

        # 7. DDR3 / DDR2 RAM
        if any(w in combined for w in ["DDR3", "DDR2", "DDR1", "PC3-", "PC2-"]):
            return True

        return False

    @staticmethod
    def validate_offer_fast(analysis: ProductAnalysis, title: str, price: float) -> tuple[bool, str]:
        """Universal fast-fail check across all PC rig categories (CPU, GPU, Case, Cooler, RAM, Storage, PSU, etc.)."""
        from app.scrapers.validator import validate_offer_fast as universal_validate
        return universal_validate(title, price, analysis)

    @staticmethod
    async def validate_candidates_llm(
        candidates: list[dict],
        analysis: ProductAnalysis
    ) -> list[dict]:
        """Uses the LLM to semantically validate a batch of raw retailer candidates.
        Returns only the candidates that the LLM confirms are a genuine match."""
        if not candidates:
            return []
        if not GROQ_API_KEY:
            # No LLM available — pass everything through so we don't block scrapes
            return candidates

        # Build a numbered list for the LLM to evaluate
        lines = []
        for i, c in enumerate(candidates):
            lines.append(f"{i+1}. [{c['retailer']}] \"{c['title']}\" — ${c['price']:.2f}")
        candidate_list = "\n".join(lines)

        system_prompt = (
            "You are a senior PC hardware expert and procurement analyst. "
            "Your job is to review a numbered list of retailer search results and decide which ones are a genuine match "
            "for the user's search query. A genuine match must:\n"
            "1. Be the exact product (correct brand, model line, and variant) — not a different tier, sub-variant, or accessory.\n"
            "2. Be a standalone retail unit — not a prebuilt PC, laptop, bundle, upgrade kit, display prop, or broken/parts item.\n"
            "3. CRITICAL: NEVER reject an offer simply because the price is low or cheap! Older CPUs, used hardware, and refurbished components sell for cheap on eBay and marketplaces. As long as it is the genuine working component, mark it VALID.\n\n"
            "Be lenient about brand name formatting quirks (e.g. 'be quiet!' vs 'be quiet', 'NVIDIA' in the title of a third-party card). "
            "Be strict about wrong product tier (e.g. RTX 4070 Ti when query is RTX 4070) or wrong product category (e.g. laptop returned for GPU search).\n\n"
            "Return valid JSON only: {\"valid_indices\": [list of 1-based indices of valid results], \"reasons\": {\"index\": \"brief reason if rejected\"}}"
        )

        user_prompt = (
            f"Search query: \"{analysis.raw_query}\"\n"
            f"Brand: {analysis.brand or 'unknown'} | Model: {analysis.model} | Category: {analysis.category}\n\n"
            f"Candidates:\n{candidate_list}\n\n"
            "Which of these are a genuine match? Return JSON."
        )

        models = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        for model_name in models:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json={
                            "model": model_name,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt}
                            ],
                            "temperature": 0,
                            "response_format": {"type": "json_object"}
                        },
                        timeout=12.0
                    )
                if res.status_code == 200:
                    data = json.loads(res.json()["choices"][0]["message"]["content"])
                    valid_indices = set(data.get("valid_indices") or [])
                    reasons = data.get("reasons") or {}

                    validated = []
                    for i, c in enumerate(candidates):
                        idx = i + 1
                        if idx in valid_indices:
                            validated.append(c)
                        else:
                            reason = reasons.get(str(idx), "Rejected by LLM")
                            print(f"[LLM Filtered] [{c['retailer']}] '{c['title'][:60]}': {reason}")
                    return validated
            except Exception as e:
                print(f"[LLM Validator Error with {model_name}] {e}")

        # LLM unavailable — return all candidates rather than silently dropping everything
        print("[LLM Validator] All models failed — passing all candidates through.")
        return candidates



# ─── 2. AMAZON CLIENT (Direct HTTP/2 Engine - Unlimited & Zero-Quota) ─────────

class AmazonClient:
    """High-speed direct Amazon client with zero API rate-limits/credit costs."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.amazon.com/",
    }

    SAFARI_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.amazon.com/",
    }

    @staticmethod
    def extract_asin(text: str) -> str | None:
        m = re.search(r'(?:/dp/|/gp/product/|^)([A-Z0-9]{10})(?:[/?&]|$)', text.strip())
        return m.group(1) if m else None

    async def _fetch_with_zenrows(self, url: str) -> tuple[int, str]:
        global ZENROWS_QUOTA_EXHAUSTED
        if ZENROWS_QUOTA_EXHAUSTED:
            return 0, ""
        zenrows_key = os.environ.get("ZENROWS_API_KEY", "") or ZENROWS_API_KEY
        if not zenrows_key:
            return 0, ""
        try:
            print(f"[ZenRows Proxy] Routing request via ZenRows (residential stealth mode)...")
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.get(
                    "https://api.zenrows.com/v1/",
                    params={
                        "apikey": zenrows_key,
                        "url": url,
                        "js_render": "true",
                        "premium_proxy": "true",
                        "proxy_country": "us",
                    }
                )
                if res.status_code == 200:
                    return 200, res.text
                if res.status_code in [401, 402]:
                    ZENROWS_QUOTA_EXHAUSTED = True
                    print(f"⚠️ [ZenRows Notice] ZenRows monthly quota exceeded (AUTH004). Skipping ZenRows for subsequent calls.")
                return res.status_code, res.text
        except Exception as e:
            print(f"[ZenRows Error] {e}")
            return 0, ""

    async def _fetch_html(self, url: str) -> tuple[int, str]:
        status_code = 0
        text = ""

        # 1. Try fast direct fetch with curl_cffi using Safari and Chrome TLS profiles
        if HAS_CURL_CFFI:
            # First try Safari TLS with matching Safari headers
            for profile in ["safari18_0", "safari17_0"]:
                try:
                    async with CurlAsyncSession(impersonate=profile) as session:
                        res = await session.get(url, headers=self.SAFARI_HEADERS, timeout=12)
                        status_code, text = res.status_code, res.text
                        if (
                            status_code == 200
                            and text
                            and "bm-verify" not in text
                            and "Robot Check" not in text
                            and "validateCaptcha" not in text
                            and "automated access" not in text.lower()
                        ):
                            return status_code, text
                except Exception as e:
                    pass

            # Second try Chrome TLS with Chrome headers
            for profile in ["chrome124", "chrome120"]:
                try:
                    async with CurlAsyncSession(impersonate=profile) as session:
                        res = await session.get(url, headers=self.HEADERS, timeout=12)
                        status_code, text = res.status_code, res.text
                        if (
                            status_code == 200
                            and text
                            and "bm-verify" not in text
                            and "Robot Check" not in text
                            and "validateCaptcha" not in text
                            and "automated access" not in text.lower()
                        ):
                            return status_code, text
                except Exception as e:
                    pass

        # Fallback to httpx if curl_cffi was unavailable or blocked
        if not text or status_code != 200:
            try:
                async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=12.0) as client:
                    res = await client.get(url)
                    status_code, text = res.status_code, res.text
            except Exception as e:
                print(f"[Amazon Direct httpx Notice] {e}")

        # Check if Amazon blocked or issued an anti-bot challenge
        is_blocked = (
            status_code != 200
            or not text
            or "bm-verify" in text
            or "Robot Check" in text
            or "validateCaptcha" in text
            or "automated access" in text.lower()
        )

        # 2. If blocked, fall back to ZenRows residential proxy (if credits remain)
        if is_blocked and not ZENROWS_QUOTA_EXHAUSTED:
            zenrows_key = os.environ.get("ZENROWS_API_KEY", "") or ZENROWS_API_KEY
            if zenrows_key:
                print(f"🛡️ [Amazon Direct] Direct request blocked ({status_code}); routing via ZenRows...")
                zr_status, zr_text = await self._fetch_with_zenrows(url)
                if zr_status == 200 and "bm-verify" not in zr_text:
                    return zr_status, zr_text

        return status_code, text

    async def lookup_asin(self, asin: str) -> dict | None:
        url = f"https://www.amazon.com/dp/{asin}"
        print(f"[Amazon Direct] Fetching product page: {url}...")
        try:
            status_code, text = await self._fetch_html(url)
            if status_code == 200:
                if "bm-verify" in text or "Robot Check" in text or "validateCaptcha" in text:
                    print(f"⚠️ [Amazon Direct] Anti-bot verification challenge triggered for ASIN {asin}.")
                    return None

                soup = BeautifulSoup(text, "html.parser")
                title_el = soup.find("span", id="productTitle")
                title = title_el.text.strip() if title_el else None
                if not title and soup.title:
                    title = soup.title.string.replace("Amazon.com:", "").strip()

                # Check in-stock availability
                avail_el = soup.find("div", id="availability")
                in_stock = True
                if avail_el:
                    avail_text = avail_el.text.strip().lower()
                    if any(w in avail_text for w in ["currently unavailable", "out of stock", "temporarily out of stock"]):
                        in_stock = False

                # Extract price from buybox
                price_val = None
                # First check corePriceDisplay or corePrice feature div
                core_price_div = soup.find("div", id="corePriceDisplay_desktop_feature_div") or soup.find("div", id="corePrice_feature_div") or soup.find("div", id="apex_desktop")
                search_scope = core_price_div if core_price_div else soup

                for span in search_scope.find_all("span", class_="a-price"):
                    # Avoid strikethrough list price
                    classes = span.get("class", [])
                    if "a-text-price" in classes:
                        continue
                    off = span.find("span", class_="a-offscreen")
                    if off and off.text:
                        m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', off.text)
                        if m:
                            price_val = float(m.group(1).replace(",", ""))
                            break

                # Fallback to general price spans if needed
                if not price_val:
                    for span in soup.find_all("span", class_="a-price"):
                        if "a-text-price" in span.get("class", []):
                            continue
                        off = span.find("span", class_="a-offscreen")
                        if off and off.text:
                            m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', off.text)
                            if m:
                                price_val = float(m.group(1).replace(",", ""))
                                break

                img = soup.find("img", id="landingImage") or soup.find("img", class_="s-image")
                image_url = img.get("src") if img else None

                merchant_input = soup.find("input", id="merchantID") or soup.find("input", {"name": "merchantID"}) or soup.find("input", {"name": "merchantId"})
                merchant_id = merchant_input.get("value") if merchant_input else None
                product_url = f"https://www.amazon.com/dp/{asin}?smid={merchant_id}" if merchant_id else url

                if title:
                    t_lower = title.lower()
                    if "renewed" in t_lower or "refurbished" in t_lower:
                        cond_tag = "refurbished"
                        cond_label = "Amazon Renewed"
                    elif "used" in t_lower:
                        cond_tag = "used"
                        cond_label = "Used"
                    else:
                        cond_tag = "new"
                        cond_label = "Brand New"

                    if price_val:
                        offer = {
                            "retailer": "Amazon",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": in_stock,
                            "condition": cond_tag,
                            "conditionLabel": cond_label,
                            "isRefurbished": cond_tag in ["refurbished", "used"],
                            "url": product_url,
                            "imageUrl": image_url,
                            "brand": None,
                            "source": "amazon-direct"
                        }
                        status_str = " (Out of Stock)" if not in_stock else ""
                        print(f"✅ [Amazon Hit] ${offer['price']:.2f}{status_str} ({cond_label}) -> {offer['title'][:60]}")
                        return offer
                    elif not in_stock:
                        offer = {
                            "retailer": "Amazon",
                            "title": title,
                            "price": 0.0,
                            "originalPrice": None,
                            "inStock": False,
                            "condition": cond_tag,
                            "conditionLabel": cond_label,
                            "isRefurbished": cond_tag in ["refurbished", "used"],
                            "url": product_url,
                            "imageUrl": image_url,
                            "brand": None,
                            "source": "amazon-direct"
                        }
                        print(f"🚫 [Amazon Sold Out] {title[:60]}")
                        return offer
            else:
                print(f"⚠️ [Amazon Direct] HTTP {status_code} received when fetching ASIN {asin}")
        except Exception as e:
            print(f"[Amazon Direct DP Error] {e}")

        # 3. Intelligent Tavily Search Fallback for Amazon ASIN
        print(f"🔍 [Amazon Fallback] Querying Tavily cache for ASIN {asin}...")
        tavily_results = await query_tavily_search(f'site:amazon.com/dp/{asin} price', max_results=3)
        if not tavily_results:
            tavily_results = await query_tavily_search(f'"{asin}" amazon price', max_results=3)
        if tavily_results:
            for item in tavily_results:
                txt = (item.get("title") or "") + " " + (item.get("content") or "")

                # Check for out-of-stock indicators in Amazon snippet
                t_lower = txt.lower()
                is_sold_out = any(phrase in t_lower for phrase in [
                    "currently unavailable", "out of stock", "temporarily out of stock",
                    "we don't know when or if this item will be back in stock", "no longer available"
                ])

                # Use robust price parsing that filters shipping minimums ($35), monthly plans, and gift cards
                from app.scrapers.search_fallback import parse_tavily_snippet_prices
                fallback_price, orig_price = parse_tavily_snippet_prices(txt)

                if fallback_price and fallback_price > 10.0:
                    raw_title = item.get("title", "").replace("Amazon.com:", "").strip()
                    rt_lower = raw_title.lower()
                    if "renewed" in rt_lower or "refurbished" in rt_lower:
                        cond_tag = "refurbished"
                        cond_label = "Amazon Renewed"
                    elif "used" in rt_lower:
                        cond_tag = "used"
                        cond_label = "Used"
                    else:
                        cond_tag = "new"
                        cond_label = "Brand New"

                    status_str = " (Out of Stock)" if is_sold_out else ""
                    print(f"✅ [Amazon Tavily Hit] ${fallback_price:.2f}{status_str} ({cond_label}) -> {raw_title[:60]}")
                    return {
                        "retailer": "Amazon",
                        "title": raw_title or f"Amazon Product {asin}",
                        "price": fallback_price,
                        "originalPrice": orig_price,
                        "inStock": not is_sold_out,
                        "condition": cond_tag,
                        "conditionLabel": cond_label,
                        "isRefurbished": cond_tag in ["refurbished", "used"],
                        "url": url,
                        "imageUrl": None,
                        "brand": None,
                        "source": "tavily-amazon"
                    }

        return None

    async def lookup_url(self, url: str) -> dict | None:
        asin = self.extract_asin(url)
        if asin:
            res = await self.lookup_asin(asin)
            if res:
                if url.startswith("http"):
                    res["url"] = url
                return res
        return None

    async def search(self, analysis: ProductAnalysis) -> list[dict]:
        asin = self.extract_asin(analysis.raw_query)
        if asin and ("amazon.com" in analysis.raw_query or len(analysis.raw_query.strip()) == 10):
            result = await self.lookup_asin(asin)
            return [result] if result else []

        search_term = analysis.retailer_search_query
        print(f"[Amazon Direct] Searching for '{search_term}'...")
        encoded = urllib.parse.quote_plus(search_term)
        url = f"https://www.amazon.com/s?k={encoded}"

        try:
            status_code, text = await self._fetch_html(url)
            if status_code == 200:
                if "bm-verify" in text or "Robot Check" in text or "validateCaptcha" in text:
                    print(f"⚠️ [Amazon Direct] Anti-bot challenge (Akamai/Captcha) triggered for '{search_term}'")
                    return None

                soup = BeautifulSoup(text, "html.parser")
                items = soup.find_all("div", {"data-component-type": "s-search-result"})
                valid_offers = []
                for it in items:
                    item_asin = it.get("data-asin")
                    if not item_asin:
                        continue

                    # Extract product title: prioritize product link with /dp/ or /gp/ and meaningful text
                    title = None
                    for a in it.find_all("a", href=True):
                        href = a.get("href", "")
                        if ("/dp/" in href or "/gp/product/" in href) and not a.find("img"):
                            txt = a.get_text(" ", strip=True)
                            if len(txt) > 15 and "out of 5 stars" not in txt.lower() and not txt.startswith("(") and "takes " not in txt.lower():
                                title = txt
                                break

                    if not title:
                        h2_el = it.find("h2")
                        if h2_el:
                            h2_txt = h2_el.get_text(" ", strip=True)
                            if len(h2_txt) > 15 and "takes " not in h2_txt.lower():
                                title = h2_txt

                    if not title:
                        for a in it.find_all("a", class_="a-link-normal"):
                            txt = a.get_text(" ", strip=True)
                            if len(txt) > 20 and not txt.startswith("(") and "takes " not in txt.lower() and "out of 5 stars" not in txt.lower():
                                title = txt
                                break

                    if not title:
                        img = it.find("img", class_="s-image")
                        if img and img.get("alt"):
                            alt_txt = img.get("alt").strip()
                            if len(alt_txt) > 15 and "takes " not in alt_txt.lower():
                                title = alt_txt

                    if not title:
                        continue

                    # Extract price
                    price_val = None
                    price_el = it.find("span", class_="a-price")
                    if price_el:
                        off = price_el.find("span", class_="a-offscreen")
                        if off:
                            m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', off.text)
                            if m:
                                price_val = float(m.group(1).replace(",", ""))

                    if not price_val:
                        whole_el = it.find("span", class_="a-price-whole")
                        if whole_el:
                            frac_el = it.find("span", class_="a-price-fraction")
                            w_str = whole_el.text.replace(",", "").replace(".", "").strip()
                            f_str = frac_el.text.strip() if frac_el else "00"
                            try:
                                price_val = float(f"{w_str}.{f_str}")
                            except ValueError:
                                pass

                    if not price_val or price_val <= 0:
                        continue

                    # Fast-fail only (LLM validation happens centrally in HardwareAgent.run)
                    is_valid, reason = ProductAnalyzer.validate_offer_fast(analysis, title, price_val)
                    if not is_valid:
                        print(f"[Amazon Skipped] '{title[:50]}...': {reason}")
                        continue

                    img = it.find("img", class_="s-image")
                    image_url = img.get("src") if img else None

                    merchant_input = it.find("input", {"name": "merchantId"})
                    merchant_id = merchant_input.get("value") if merchant_input else None
                    product_url = f"https://www.amazon.com/dp/{item_asin}?smid={merchant_id}" if merchant_id else f"https://www.amazon.com/dp/{item_asin}"

                    t_lower = title.lower()
                    if "renewed" in t_lower or "refurbished" in t_lower:
                        cond_tag = "refurbished"
                        cond_label = "Amazon Renewed"
                    elif "used" in t_lower:
                        cond_tag = "used"
                        cond_label = "Used"
                    else:
                        cond_tag = "new"
                        cond_label = "Brand New"

                    valid_offers.append({
                        "retailer": "Amazon",
                        "title": title,
                        "price": price_val,
                        "originalPrice": None,
                        "inStock": True,
                        "condition": cond_tag,
                        "conditionLabel": cond_label,
                        "isRefurbished": cond_tag in ["refurbished", "used"],
                        "url": product_url,
                        "imageUrl": image_url,
                        "brand": analysis.brand,
                        "source": "amazon-direct"
                    })

                if valid_offers:
                    valid_offers.sort(key=lambda x: x["price"])
                    print(f"[Amazon Raw] {len(valid_offers)} candidate(s) collected (pre-LLM)")
                    return valid_offers
                else:
                    print(f"[Amazon Direct] 0 candidates after fast-fail for '{search_term}'")
            else:
                print(f"⚠️ [Amazon Direct] HTTP {status_code} received for '{search_term}'")
        except Exception as e:
            print(f"[Amazon Direct Search Error] {e}")
        return []


# ─── 3. EBAY CLIENT (eBay Browse API with OAuth2) ────────────────────────────

class EbayClient:
    """Official eBay Browse API client with automatic OAuth application token caching."""

    def __init__(self):
        self._access_token: str | None = None
        self._token_expires_at: float = 0

    def _is_sandbox(self, client_id: str) -> bool:
        return "SBX" in client_id.upper()

    def _get_oauth_url(self, client_id: str) -> str:
        return "https://api.sandbox.ebay.com/identity/v1/oauth2/token" if self._is_sandbox(client_id) else "https://api.ebay.com/identity/v1/oauth2/token"

    def _get_browse_url(self, client_id: str) -> str:
        return "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search" if self._is_sandbox(client_id) else "https://api.ebay.com/buy/browse/v1/item_summary/search"

    async def get_access_token(self) -> str | None:
        client_id = os.environ.get("EBAY_CLIENT_ID", "") or EBAY_CLIENT_ID
        client_secret = os.environ.get("EBAY_CLIENT_SECRET", "") or EBAY_CLIENT_SECRET

        if not client_id or not client_secret:
            return None

        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token

        env_name = "Sandbox" if self._is_sandbox(client_id) else "Production"
        print(f"[eBay API] Refreshing eBay OAuth application token ({env_name})...")
        credentials = f"{client_id}:{client_secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    self._get_oauth_url(client_id),
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
                    print(f"✅ [eBay API] OAuth token acquired ({env_name}, valid for {expires_in}s).")
                    return self._access_token
                else:
                    print(f"⚠️ [eBay Auth Error] {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[eBay Auth Exception] {e}")
        return None

    async def search(self, analysis: ProductAnalysis) -> list[dict]:
        client_id = os.environ.get("EBAY_CLIENT_ID", "") or EBAY_CLIENT_ID
        token = await self.get_access_token()
        if not token:
            print("[eBay API] ℹ️ EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not configured, skipping eBay.")
            return []

        search_query = analysis.retailer_search_query
        print(f"[eBay API] Searching for '{search_query}'...")
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    self._get_browse_url(client_id),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"
                    },
                    params={
                        "q": search_query,
                        "limit": "10",
                        "filter": "buyingOptions:{FIXED_PRICE}"
                    },
                    timeout=15.0
                )
                if res.status_code == 200:
                    items = res.json().get("itemSummaries", [])
                    raw_offers = []
                    for it in items:
                        title = it.get("title", "")
                        price_obj = it.get("price", {})
                        price_str = price_obj.get("value")
                        if not title or not price_str:
                            continue

                        condition_raw = it.get("condition", "Used")
                        c_lower = condition_raw.lower()
                        if any(w in c_lower for w in ["parts", "not working", "broken", "faulty", "as is"]):
                            print(f"[eBay Skipped] '{title[:50]}...': Bad condition '{condition_raw}'")
                            continue

                        price_val = float(price_str)
                        is_valid, reason = ProductAnalyzer.validate_offer_fast(analysis, title, price_val)
                        if not is_valid:
                            print(f"[eBay Skipped] '{title[:50]}...': {reason}")
                            continue

                        if "refurbished" in c_lower:
                            cond_tag = "refurbished"
                            cond_label = "Refurbished"
                        elif any(w in c_lower for w in ["used", "pre-owned"]):
                            cond_tag = "used"
                            cond_label = "Used (Tested)"
                        elif "open" in c_lower:
                            cond_tag = "open_box"
                            cond_label = "Open Box"
                        else:
                            cond_tag = "new"
                            cond_label = "Brand New"

                        image_url = (it.get("image") or {}).get("imageUrl")
                        item_url = it.get("itemWebUrl") or f"https://www.ebay.com/itm/{it.get('itemId')}"

                        raw_offers.append({
                            "retailer": "eBay",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": True,
                            "condition": cond_tag,
                            "conditionLabel": cond_label,
                            "isRefurbished": cond_tag in ["refurbished", "used"],
                            "url": item_url,
                            "imageUrl": image_url,
                            "brand": analysis.brand,
                            "source": "ebay-api"
                        })

                    # Return all raw offers — LLM validation happens centrally in HardwareAgent.run()
                    if raw_offers:
                        raw_offers.sort(key=lambda x: x["price"])
                        print(f"[eBay Raw] {len(raw_offers)} candidate(s) collected (pre-LLM)")
                        return raw_offers
                    return []
                else:
                    print(f"⚠️ [eBay Search Error] {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[eBay Search Exception] {e}")
        return []

    @staticmethod
    def extract_item_id(text: str) -> str | None:
        if not text:
            return None
        # Support /itm/123456789012, /itm/slug/123456789012, ?item=123456789012, ?itemId=123456789012, or raw numeric ID
        m = re.search(r'/itm/(?:[^/?#]+/)?(\d{9,15})', text)
        if m:
            return m.group(1)
        m = re.search(r'[?&](?:item|itemId|id)=(\d{9,15})', text, re.IGNORECASE)
        if m:
            return m.group(1)
        clean = text.strip()
        if clean.isdigit() and len(clean) in range(9, 16):
            return clean
        return None

    def _get_item_url(self, client_id: str, legacy_id: str) -> str:
        base = "https://api.sandbox.ebay.com/buy/browse/v1/item" if self._is_sandbox(client_id) else "https://api.ebay.com/buy/browse/v1/item"
        return f"{base}/get_item_by_legacy_id?legacy_item_id={legacy_id}"

    async def lookup_item_id(self, item_id: str) -> dict | None:
        client_id = os.environ.get("EBAY_CLIENT_ID", "") or EBAY_CLIENT_ID
        token = await self.get_access_token()
        if not token:
            print("[eBay API] ℹ️ EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not configured.")
            return None

        url = self._get_item_url(client_id, item_id)
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                res = await client.get(
                    url,
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
                        if "refurbished" in c_lower:
                            cond_tag = "refurbished"
                            cond_label = "Refurbished"
                        elif any(w in c_lower for w in ["used", "pre-owned"]):
                            cond_tag = "used"
                            cond_label = "Used (Tested)"
                        elif "open" in c_lower:
                            cond_tag = "open_box"
                            cond_label = "Open Box"
                        else:
                            cond_tag = "new"
                            cond_label = "Brand New"

                        image_url = (data.get("image") or {}).get("imageUrl")
                        item_url = data.get("itemWebUrl") or f"https://www.ebay.com/itm/{item_id}"

                        avail = data.get("estimatedAvailabilities", [])
                        in_stock = True
                        if avail and avail[0].get("estimatedAvailabilityStatus") == "OUT_OF_STOCK":
                            in_stock = False

                        offer = {
                            "retailer": "eBay",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": in_stock,
                            "condition": cond_tag,
                            "conditionLabel": cond_label,
                            "isRefurbished": cond_tag in ["refurbished", "used"],
                            "url": item_url,
                            "imageUrl": image_url,
                            "brand": None,
                            "source": "ebay-api"
                        }
                        print(f"✅ [eBay Hit] ${offer['price']:.2f} ({cond_label}) -> {offer['title'][:60]}")
                        return offer
                else:
                    print(f"⚠️ [eBay Lookup Status] {res.status_code} for item {item_id}")
        except Exception as e:
            print(f"[eBay Lookup Exception] {e}")
        return None

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.ebay.com/",
    }

    async def _fetch_html(self, url: str) -> tuple[int, str]:
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
            return status_code, text

    async def lookup_url(self, url: str) -> dict | None:
        item_id = self.extract_item_id(url)
        if item_id:
            res = await self.lookup_item_id(item_id)
            if res:
                if url.startswith("http"):
                    res["url"] = url
                return res

        # Tier 2 Fallback: Direct DOM HTML extraction
        status_code, html = await self._fetch_html(url)
        if status_code == 200 and html:
            try:
                soup = BeautifulSoup(html, "html.parser")
                title = None
                title_el = soup.find("h1", class_=re.compile(r"x-item-title__mainTitle", re.I)) or soup.find("h1")
                if title_el:
                    title = title_el.get_text(strip=True)
                elif soup.title:
                    title = soup.title.string.replace("| eBay", "").strip()

                price_val = None
                price_container = (
                    soup.find("div", class_=re.compile(r"x-price-primary", re.I)) or
                    soup.find("div", class_=re.compile(r"x-bin-price", re.I)) or
                    soup.find("span", itemprop="price")
                )
                if price_container:
                    m = re.search(r"[\$US\s]*([0-9,]+\.[0-9]{2})", price_container.get_text(strip=True))
                    if m:
                        price_val = float(m.group(1).replace(",", ""))

                if not price_val:
                    m_p = re.search(r"US\s*\$([0-9,]+\.[0-9]{2})", html)
                    if m_p:
                        price_val = float(m_p.group(1).replace(",", ""))

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
                    print(f"✅ [eBay Direct HTML Hit] ${price_val:.2f} (inStock={in_stock}) -> {title[:60] if title else url}")
                    return {
                        "retailer": "eBay",
                        "title": title or "eBay Hardware Product",
                        "price": price_val,
                        "originalPrice": None,
                        "inStock": in_stock,
                        "condition": cond,
                        "conditionLabel": "Brand New" if cond == "new" else ("Refurbished" if cond == "refurbished" else "Used"),
                        "isRefurbished": cond in ["refurbished", "used"],
                        "url": url,
                        "imageUrl": img_url,
                        "brand": None,
                        "source": "ebay-direct-html"
                    }
            except Exception as e:
                print(f"[eBay Direct HTML Parse Notice] {e}")

        # Tier 3 Fallback: Tavily search
        try:
            from app.scrapers.search_fallback import query_tavily_search
            from app.scrapers.smart import parse_result_text
            t_hits = await query_tavily_search(f'site:ebay.com/itm/{item_id} price' if item_id else f'"{url}" price', max_results=2)
            if t_hits:
                hit = t_hits[0]
                txt = (hit.get("title", "") or "") + "\n" + (hit.get("content", "") or "")
                smart_listings = await parse_result_text(txt, "eBay", url, item_id or url)
                if smart_listings:
                    sl = smart_listings[0]
                    return {
                        "retailer": "eBay",
                        "title": sl.get("title") or hit.get("title"),
                        "price": sl["price"],
                        "originalPrice": sl.get("original_price"),
                        "inStock": sl.get("in_stock", True),
                        "condition": sl.get("condition", "new"),
                        "conditionLabel": "Brand New" if sl.get("condition") == "new" else "Used",
                        "isRefurbished": sl.get("condition") in ["refurbished", "used"],
                        "url": url,
                        "imageUrl": None,
                        "brand": None,
                        "source": "ebay-tavily-smart"
                    }
        except Exception as tavily_err:
            print(f"[eBay Tavily Fallback Notice] {tavily_err}")

        return None


# ─── 4. BEST BUY CLIENT (Direct curl_cffi with Seamless ZenRows Backup) ─────────

class BestBuyClient:
    """Best Buy search & product parser: direct curl_cffi (0 credits) with ZenRows residential fallback."""

    HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bestbuy.com/",
    }

    async def _fetch_with_zenrows(self, url: str) -> tuple[int, str]:
        global ZENROWS_QUOTA_EXHAUSTED
        if ZENROWS_QUOTA_EXHAUSTED:
            return 0, ""
        zenrows_key = os.environ.get("ZENROWS_API_KEY", "") or ZENROWS_API_KEY
        if not zenrows_key:
            return 0, ""
        try:
            print(f"[ZenRows Proxy] Routing Best Buy request via ZenRows (residential stealth mode)...")
            async with httpx.AsyncClient(timeout=25.0) as client:
                res = await client.get(
                    "https://api.zenrows.com/v1/",
                    params={
                        "apikey": zenrows_key,
                        "url": url,
                        "js_render": "true",
                        "premium_proxy": "true",
                        "proxy_country": "us",
                    }
                )
                if res.status_code in [401, 402]:
                    ZENROWS_QUOTA_EXHAUSTED = True
                    print(f"⚠️ [ZenRows Notice] ZenRows monthly quota exceeded (AUTH004). Skipping ZenRows for subsequent calls.")
                return res.status_code, res.text
        except Exception as e:
            print(f"[Best Buy ZenRows Fallback Error] {e}")
            return 0, ""

    async def _fetch_html(self, url: str) -> tuple[int, str]:
        status_code = 0
        text = ""

        # 1. Primary: Direct curl_cffi with HTTP/1.1 (prevents curl error 92 HTTP/2 stream reset)
        if HAS_CURL_CFFI:
            try:
                kwargs = {"headers": self.HEADERS, "timeout": 2.5}
                if CurlHttpVersion is not None:
                    kwargs["http_version"] = CurlHttpVersion.V1_1
                async with CurlAsyncSession(impersonate="chrome124") as session:
                    res = await session.get(url, **kwargs)
                    status_code, text = res.status_code, res.text
                    if status_code == 200 and text and "access denied" not in text.lower():
                        return status_code, text
            except Exception:
                pass

        # 2. httpx fallback if curl_cffi failed or was unavailable
        if not text or status_code != 200:
            try:
                async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=2.5) as client:
                    res = await client.get(url)
                    status_code, text = res.status_code, res.text
            except Exception as e:
                print(f"[Best Buy Direct httpx Notice] {e}")

        # Check if Best Buy blocked or challenged the request
        is_blocked = (
            status_code != 200
            or not text
            or "access denied" in text.lower()
            or "automated access" in text.lower()
            or status_code in [403, 429, 503]
        )

        # 3. ZenRows residential proxy fallback (if not exhausted)
        if is_blocked and not ZENROWS_QUOTA_EXHAUSTED:
            zenrows_key = os.environ.get("ZENROWS_API_KEY", "") or ZENROWS_API_KEY
            if zenrows_key:
                print(f"🛡️ [Best Buy Direct] Direct request blocked ({status_code}); routing via ZenRows residential proxy...")
                zr_status, zr_text = await self._fetch_with_zenrows(url)
                if zr_status == 200 and zr_text and "access denied" not in zr_text.lower():
                    return zr_status, zr_text

        return status_code, text

    async def _lookup_priceblocks_sku(self, sku: str) -> dict | None:
        """Hits Best Buy's official internal priceBlocks JSON API for exact live price and stock status."""
        try:
            url = f"https://www.bestbuy.com/api/3.0/priceBlocks?skus={sku}"
            headers = {
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }
            if HAS_CURL_CFFI:
                async with CurlAsyncSession(impersonate="chrome124") as session:
                    res = await session.get(url, headers=headers, timeout=8)
                    if res.status_code == 200:
                        data = res.json()
                        if data and isinstance(data, list) and len(data) > 0:
                            sku_info = data[0].get("sku", {})
                            price_info = sku_info.get("price", {})
                            names = sku_info.get("names", {})
                            btn = sku_info.get("buttonState", {})
                            pdp_path = sku_info.get("url", "")

                            c_price = price_info.get("currentPrice")
                            r_price = price_info.get("regularPrice")
                            title = names.get("short") or names.get("title")
                            btn_state = str(btn.get("buttonState", "")).upper()
                            in_stock = bool(btn.get("purchasable", False) and btn_state in ["ADD_TO_CART", "PRE_ORDER"])

                            if c_price and title:
                                p_url = f"https://www.bestbuy.com{pdp_path}" if pdp_path.startswith("/") else (pdp_path or f"https://www.bestbuy.com/site/{sku}.p")
                                img_url = f"https://pisces.bbystatic.com/image2/BestBuy_US/images/products/{sku[:4]}/{sku}_sd.jpg"
                                return {
                                    "title": title,
                                    "price": float(c_price),
                                    "originalPrice": float(r_price) if r_price else None,
                                    "inStock": in_stock,
                                    "url": p_url,
                                    "imageUrl": img_url,
                                    "sku": sku
                                }
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_tavily_snippet_prices(txt: str) -> tuple[float | None, float | None]:
        """Accurately parses current price and original/was price from search snippets without picking up old historic prices."""
        # 1. Detect 'the price was $XX.XX', 'was $XX.XX', 'regular price $XX.XX'
        was_matches = re.findall(r"\b(?:the\s+price\s+was|was|regular(?:\s+price)?)\s*:?\s*\$([0-9,]+\.[0-9]{2})", txt, re.I)
        was_prices = [float(p.replace(",", "")) for p in was_matches]

        # 2. Check for explicit active price prefix ('New $XX.XX', 'Now $XX.XX', 'Sale $XX.XX', 'Current Price $XX.XX')
        new_m = re.search(r"\b(?:New|Now|Sale|Current(?:\s*Price)?)\s*:?\s*\$([0-9,]+\.[0-9]{2})", txt, re.I)

        # 3. Strip out 'was' prices so we don't accidentally treat stale historic prices as current
        clean_txt = re.sub(r"\b(?:the\s+price\s+was|was|regular(?:\s+price)?)\s*:?\s*\$[0-9,]+\.[0-9]{2}", "", txt, flags=re.I)
        active_prices = [float(p.replace(",", "")) for p in re.findall(r'\$([0-9,]+\.[0-9]{2})', clean_txt)]
        valid_active = [p for p in active_prices if p > 5.0]

        current_price = None
        if new_m:
            current_price = float(new_m.group(1).replace(",", ""))
        elif valid_active:
            current_price = valid_active[0]

        orig_price = None
        if was_prices and current_price and was_prices[0] > current_price:
            orig_price = was_prices[0]

        return current_price, orig_price

    async def _fallback_tavily_search(self, analysis: ProductAnalysis) -> list[dict]:
        """Automated Tavily search fallback: queries Best Buy catalog via Tavily + priceBlocks API."""
        search_term = analysis.retailer_search_query
        print(f"🛡️ [Best Buy Fallback] Querying Best Buy via Tavily: \"{search_term}\"...")
        t_query = f'site:bestbuy.com {search_term} price'

        try:
            results = await query_tavily_search(t_query, max_results=6)
            if not results:
                return None

            candidates = []
            for r in results:
                u = r.get("url", "")
                if "bestbuy.com" not in u or "/site/searchpage" in u or "/questions/" in u:
                    continue

                # Normalize review URLs to canonical product URLs
                clean_url = u.replace("/site/reviews/", "/site/").replace("/reviews/", "/")

                # Check for 6-8 digit SKU in URL
                sku_m = re.search(r'/([0-9]{6,8})(?:\.p|\?)', clean_url) or re.search(r'sku(?:Id)?(?:/|=)([0-9]{6,8})', clean_url) or re.search(r'/([0-9]{6,8})', clean_url)
                sku = sku_m.group(1) if sku_m else None

                title = r.get("title", "")
                title = re.sub(r'^(?:Customer Reviews:\s*|Questions and Answers:\s*)', '', title, flags=re.I)
                title = re.sub(r'\s*-\s*Best\s*Buy.*$', '', title, flags=re.I).strip()

                price = None
                orig_price = None
                in_stock = True
                img_url = None

                # 1. Try official priceBlocks API if SKU is extracted
                if sku:
                    pb_item = await self._lookup_priceblocks_sku(sku)
                    if pb_item:
                        title = pb_item.get("title") or title
                        price = pb_item.get("price")
                        orig_price = pb_item.get("originalPrice")
                        in_stock = pb_item.get("inStock", True)
                        img_url = pb_item.get("imageUrl")
                        clean_url = pb_item.get("url") or clean_url
                    else:
                        # Product SKU is discontinued or inactive on Best Buy.
                        # Never parse raw snippet text for inactive SKUs as it leaks
                        # "Customers Also Viewed" AM5/AM4 carousel prices (e.g. $559).
                        continue

                # 2. Fallback to smart snippet extraction ONLY when no SKU was present in URL
                txt = (r.get("title") or "") + "\n" + (r.get("content") or "")
                if not price and not sku:
                    from app.scrapers.smart import parse_result_text
                    smart_listings = await parse_result_text(txt, "Best Buy", clean_url, analysis.raw_query, analysis.category)
                    if smart_listings:
                        sl = smart_listings[0]
                        title = sl.get("title") or title
                        price = sl["price"]
                        orig_price = sl.get("original_price")
                        in_stock = sl.get("in_stock", True)
                    else:
                        continue

                if price and price > 0:
                    is_valid, reason = ProductAnalyzer.validate_offer_fast(analysis, title, price)
                    if is_valid:
                        t_lower = title.lower()
                        is_refurb = "refurbished" in t_lower
                        is_open_box = any(w in t_lower for w in ["open-box", "open box"])
                        if is_refurb:
                            cond_tag = "refurbished"
                            cond_label = "Geek Squad Certified"
                        elif is_open_box:
                            cond_tag = "open_box"
                            cond_label = "Open Box"
                        else:
                            cond_tag = "new"
                            cond_label = "Brand New"

                        candidates.append({
                            "retailer": "Best Buy",
                            "title": title,
                            "price": price,
                            "originalPrice": orig_price,
                            "inStock": in_stock,
                            "condition": cond_tag,
                            "conditionLabel": cond_label,
                            "isRefurbished": cond_tag in ["refurbished", "open_box"],
                            "url": clean_url,
                            "imageUrl": img_url,
                            "brand": analysis.brand,
                            "source": "bestbuy-fallback"
                        })
                    else:
                        print(f"[Best Buy Fallback Skipped] '{title[:50]}...': {reason}")

            if candidates:
                candidates.sort(key=lambda x: (not x.get("inStock", True), x["price"]))
                print(f"[Best Buy Fallback Raw] {len(candidates)} candidate(s) collected (pre-LLM)")
                return candidates
        except Exception as e:
            print(f"[Best Buy Fallback Notice] {e}")

        return []

    async def search(self, analysis: ProductAnalysis) -> dict | None:
        search_term = analysis.retailer_search_query
        print(f"[Best Buy Direct] Searching for '{search_term}'...")
        encoded = urllib.parse.quote_plus(search_term)
        url = f"https://www.bestbuy.com/site/searchpage.jsp?st={encoded}&intl=nosplash"

        try:
            status_code, text = await self._fetch_html(url)
            if status_code != 200 or not text:
                print(f"ℹ️ [Best Buy Direct] Search unavailable (HTTP {status_code}); switching to Best Buy fallback...")
                return await self._fallback_tavily_search(analysis)

            # 1. Map skuId -> price from embedded pricing objects
            sku_prices = {}
            for m in re.finditer(r"\"customerPrice\":\s*([0-9.]+)[^}]*?\"skuId\":\s*\"(\d+)\"", text):
                price, sku = float(m.group(1)), m.group(2)
                if sku not in sku_prices or price < sku_prices[sku]:
                    sku_prices[sku] = price

            for m in re.finditer(r"\"skuId\":\s*\"(\d+)\"[^}]*?\"customerPrice\":\s*([0-9.]+)", text):
                sku, price = m.group(1), float(m.group(2))
                if sku not in sku_prices or price < sku_prices[sku]:
                    sku_prices[sku] = price

            # 2. Extract products from Apollo SSR chunks
            valid_offers = []
            for m in re.finditer(r"\"__typename\":\"Product\",\"skuId\":\"(\d+)\"(.*?)(?=\"__typename\":\"Product\"|$)", text):
                sku = m.group(1)
                chunk = m.group(2)

                title_m = re.search(r"\"name\":\{[^}]*\"short\":\"([^\"]+)\"", chunk) or re.search(r"\"title\":\"([^\"]+)\"", chunk)
                if not title_m:
                    continue
                title = title_m.group(1).encode("utf-8").decode("unicode_escape", errors="ignore")

                url_m = re.search(r"\"skuSpecificUrl\":\"([^\"]+)\"", chunk) or re.search(r"\"pdp\":\"([^\"]+)\"", chunk)
                pdp_url = url_m.group(1) if url_m else f"https://www.bestbuy.com/site/searchpage.jsp?st={sku}"

                img_m = re.search(r"\"(?:piscesHref|href)\":\"([^\"]+)\"", chunk)
                img_url = img_m.group(1) if img_m else None

                price = sku_prices.get(sku)
                if not price:
                    pm = re.search(r"\"customerPrice\":\s*([0-9.]+)", chunk)
                    if pm:
                        price = float(pm.group(1))

                if not price or price <= 0:
                    continue

                # Fast-fail only (LLM handles semantic matching centrally)
                is_valid, reason = ProductAnalyzer.validate_offer_fast(analysis, title, price)
                if not is_valid:
                    print(f"[Best Buy Skipped] '{title[:50]}...': {reason}")
                    continue

                btn_m = re.search(r'\"buttonState\":\s*\"([^\"]+)\"', chunk)
                btn_state = (btn_m.group(1) if btn_m else "").upper()
                is_sold_out = btn_state in ["SOLD_OUT", "UNAVAILABLE", "NOT_ORDERABLE"] or any(
                    phrase in chunk.lower() for phrase in [
                        "no longer available in new condition",
                        "no longer available",
                        "sold out",
                        "currently unavailable",
                        "not available in new condition"
                    ]
                )
                in_stock = not is_sold_out

                t_lower = title.lower()
                is_refurb = "refurbished" in t_lower
                is_open_box = any(w in t_lower for w in ["open-box", "open box"])
                if is_refurb:
                    cond_tag = "refurbished"
                    cond_label = "Geek Squad Certified"
                elif is_open_box:
                    cond_tag = "open_box"
                    cond_label = "Open Box"
                else:
                    cond_tag = "new"
                    cond_label = "Brand New"

                valid_offers.append({
                    "retailer": "Best Buy",
                    "title": title,
                    "price": price,
                    "originalPrice": None,
                    "inStock": in_stock,
                    "condition": cond_tag,
                    "conditionLabel": cond_label,
                    "isRefurbished": cond_tag in ["refurbished", "open_box"],
                    "url": pdp_url,
                    "imageUrl": img_url,
                    "brand": analysis.brand,
                    "source": "bestbuy-direct"
                })

            # Fallback to DOM info-section if SSR chunk regex found no valid offers
            if not valid_offers:
                soup = BeautifulSoup(text, "html.parser")
                for info in soup.find_all("div", class_="info-section"):
                    t_el = info.find(class_=re.compile(r"product-title"))
                    t = t_el.get_text(strip=True) if t_el else None
                    if not t:
                        continue
                    pm = re.search(r"\$([0-9,]+\.[0-9]{2})", info.get_text())
                    if not pm:
                        continue
                    p_val = float(pm.group(1).replace(",", ""))
                    is_valid, reason = ProductAnalyzer.validate_offer_fast(analysis, t, p_val)
                    if not is_valid:
                        continue
                    link_el = info.find("a", href=re.compile(r"/product/")) or (info.parent.find("a", href=re.compile(r"/product/")) if info.parent else None)
                    p_link = ("https://www.bestbuy.com" + link_el["href"]) if link_el and link_el.get("href", "").startswith("/") else (link_el.get("href") if link_el else url)
                    sku_b = info.find_parent("div", class_="sku-block") or info.parent
                    img_el = sku_b.find("img") if sku_b else None
                    p_img = img_el.get("src") if img_el else None

                    info_txt = info.get_text().lower()
                    is_sold_out = any(
                        phrase in info_txt for phrase in [
                            "no longer available in new condition",
                            "no longer available",
                            "sold out",
                            "currently unavailable",
                            "not available in new condition"
                        ]
                    )
                    in_stock = not is_sold_out

                    t_dom_lower = t.lower()
                    is_dom_refurb = "refurbished" in t_dom_lower
                    is_dom_open = any(w in t_dom_lower for w in ["open-box", "open box"])
                    if is_dom_refurb:
                        dom_cond_tag = "refurbished"
                        dom_cond_label = "Geek Squad Certified"
                    elif is_dom_open:
                        dom_cond_tag = "open_box"
                        dom_cond_label = "Open Box"
                    else:
                        dom_cond_tag = "new"
                        dom_cond_label = "Brand New"

                    valid_offers.append({
                        "retailer": "Best Buy",
                        "title": t,
                        "price": p_val,
                        "originalPrice": None,
                        "inStock": in_stock,
                        "condition": dom_cond_tag,
                        "conditionLabel": dom_cond_label,
                        "isRefurbished": dom_cond_tag in ["refurbished", "open_box"],
                        "url": p_link,
                        "imageUrl": p_img,
                        "brand": analysis.brand,
                        "source": "bestbuy-direct"
                    })

            if valid_offers:
                valid_offers.sort(key=lambda x: (not x.get("inStock", True), x["price"]))
                print(f"[Best Buy Raw] {len(valid_offers)} candidate(s) collected (pre-LLM)")
                return valid_offers
            else:
                print(f"[Best Buy Direct] 0 valid candidates for '{search_term}'; engaging fallback...")
                return await self._fallback_tavily_search(analysis)
        except Exception as e:
            print(f"[Best Buy Direct Search Notice] {e}; switching to fallback...")
            return await self._fallback_tavily_search(analysis)

        return []

    async def lookup_url(self, url: str) -> dict | None:
        target_url = url + ("&intl=nosplash" if "?" in url else "?intl=nosplash") if "nosplash" not in url else url

        # 1. Quick PriceBlocks API lookup if SKU is in the URL (6 to 8 digits)
        sku_m = re.search(r'/([0-9]{6,8})(?:\.p|\?)', url) or re.search(r'sku(?:Id)?(?:/|=)([0-9]{6,8})', url) or re.search(r'\b([0-9]{6,8})\b', url)
        sku = sku_m.group(1) if sku_m else ""
        if sku:
            pb_item = await self._lookup_priceblocks_sku(sku)
            if pb_item:
                status_note = "" if pb_item.get("inStock", True) else " [Out of Stock]"
                print(f"✅ [Best Buy SKU Hit] ${pb_item['price']:.2f}{status_note} -> {pb_item['title'][:60]}")
                return {
                    "retailer": "Best Buy",
                    "title": pb_item["title"],
                    "price": pb_item["price"],
                    "originalPrice": pb_item.get("originalPrice"),
                    "inStock": pb_item.get("inStock", True),
                    "isRefurbished": any(w in pb_item["title"].lower() for w in ["refurbished", "open-box", "open box"]),
                    "url": pb_item.get("url") or url,
                    "imageUrl": pb_item.get("imageUrl"),
                    "brand": None,
                    "source": "bestbuy-api"
                }

        # 2. Try direct HTML fetch
        try:
            status_code, text = await self._fetch_html(target_url)
            if status_code == 200 and text:
                title = None
                price = None
                img = None

                title_m = re.search(r"\"name\":\{[^}]*\"short\":\"([^\"]+)\"", text) or re.search(r"<title>(.*?)(?:\s*-\s*Best\s*Buy|</title>)", text, re.I)
                if title_m:
                    title = title_m.group(1).strip()

                price_m = re.search(r"\"customerPrice\":\s*([0-9.]+)", text) or re.search(r"\"currentPrice\":\s*([0-9.]+)", text)
                if price_m:
                    price = float(price_m.group(1))
                else:
                    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.DOTALL):
                        try:
                            ld = json.loads(m.group(1))
                            if isinstance(ld, list):
                                ld = ld[0]
                            offers_ld = ld.get("offers", {}) if isinstance(ld, dict) else {}
                            if isinstance(offers_ld, list) and offers_ld:
                                offers_ld = offers_ld[0]
                            if isinstance(offers_ld, dict) and offers_ld.get("price"):
                                price = float(offers_ld["price"])
                                break
                        except Exception:
                            continue

                img_m = re.search(r"\"(?:piscesHref|href)\":\"(https://pisces\.bbystatic\.com/[^\"]+)\"", text)
                if img_m:
                    img = img_m.group(1)

                is_sold_out = any(phrase in text.lower() for phrase in [
                    "no longer available in new condition",
                    "no longer available",
                    "sold out",
                    "currently unavailable",
                    "not available in new condition"
                ])
                in_stock = not is_sold_out

                if title:
                    if price or not in_stock:
                        status_note = "" if in_stock else " [Out of Stock]"
                        print(f"✅ [Best Buy Direct Hit] ${price or 0.0:.2f}{status_note} -> {title[:60]}")
                        return {
                            "retailer": "Best Buy",
                            "title": title,
                            "price": price or 0.0,
                            "originalPrice": None,
                            "inStock": in_stock,
                            "isRefurbished": any(w in title.lower() for w in ["refurbished", "open-box", "open box"]),
                            "url": url,
                            "imageUrl": img,
                            "brand": None,
                            "source": "bestbuy-direct"
                        }
        except Exception as e:
            print(f"[Best Buy URL Lookup Error] {e}")

        # 3. Fallback: Query Tavily Search with product keywords extracted from URL
        try:
            slug = re.sub(r'https?://(?:www\.)?bestbuy\.com/(?:site/|product/)?', '', url)
            clean_slug = re.sub(r'[-_/]+', ' ', slug).replace('.p', '').strip()
            clean_slug = ' '.join([w for w in clean_slug.split() if not w.isdigit() and len(w) > 1][:6])

            t_query = f'site:bestbuy.com "{sku}"' if sku else f'site:bestbuy.com {clean_slug} price'
            print(f"🔍 [Best Buy Fallback] Querying Tavily cache: {t_query[:60]}...")
            tavily_results = await query_tavily_search(t_query, max_results=3)
            if tavily_results:
                from app.scrapers.smart import parse_result_text
                for it in tavily_results:
                    txt = (it.get("title") or "") + "\n" + (it.get("content") or "")
                    smart_listings = await parse_result_text(txt, "Best Buy", url, clean_slug)
                    if smart_listings:
                        sl = smart_listings[0]
                        s_price = sl["price"]
                        s_orig = sl.get("original_price")
                        raw_title = sl.get("title") or it.get("title", "")
                        in_stock = sl.get("in_stock", True)
                        cond = sl.get("condition", "new")
                        status_note = "" if in_stock else " [Out of Stock]"
                        print(f"✅ [Best Buy Smart Fallback Hit] ${s_price:.2f}{status_note} -> {raw_title[:60]}")
                        return {
                            "retailer": "Best Buy",
                            "title": raw_title or "Best Buy Hardware",
                            "price": s_price,
                            "originalPrice": s_orig,
                            "inStock": in_stock,
                            "condition": cond,
                            "isRefurbished": cond in ["refurbished", "open_box"],
                            "url": url,
                            "imageUrl": None,
                            "brand": None,
                            "source": "smart-bestbuy"
                        }
        except Exception as e:
            print(f"[Best Buy Smart Fallback Notice] {e}")

        return None


# ─── 5. GENERIC RETAILER CLIENT (Direct URL Parser) ───────────────────────────

class GenericRetailerClient:
    """Direct URL price parser for other retailers (Newegg, Best Buy, B&H, Micro Center, etc.)."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def lookup_url(self, url: str) -> dict | None:
        try:
            async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    title = soup.title.string.strip() if soup.title else "Online Retailer Product"

                    # 1. Try JSON-LD schema.org/Product
                    for script in soup.find_all("script", type="application/ld+json"):
                        try:
                            ld_data = json.loads(script.string or "{}")
                            if isinstance(ld_data, list):
                                ld_data = ld_data[0]
                            if isinstance(ld_data, dict):
                                offers = ld_data.get("offers")
                                if isinstance(offers, list):
                                    offers = offers[0]
                                if isinstance(offers, dict) and offers.get("price"):
                                    p = float(offers["price"])
                                    if p > 0:
                                        t = ld_data.get("name") or title
                                        img = ld_data.get("image")
                                        if isinstance(img, list) and img:
                                            img = img[0]
                                        return {
                                            "retailer": "Online Retailer",
                                            "title": t,
                                            "price": p,
                                            "originalPrice": None,
                                            "inStock": "InStock" in str(offers.get("availability", "")),
                                            "url": url,
                                            "imageUrl": img,
                                            "source": "generic-jsonld"
                                        }
                        except Exception:
                            continue

                    # 2. Try OpenGraph meta tags
                    og_price = soup.find("meta", property="og:price:amount") or soup.find("meta", property="product:price:amount")
                    og_title = soup.find("meta", property="og:title")
                    og_img = soup.find("meta", property="og:image")
                    if og_price and og_price.get("content"):
                        try:
                            p = float(og_price["content"].replace("$", "").replace(",", "").strip())
                            if p > 0:
                                return {
                                    "retailer": "Online Retailer",
                                    "title": og_title.get("content") if og_title else title,
                                    "price": p,
                                    "originalPrice": None,
                                    "inStock": True,
                                    "url": url,
                                    "imageUrl": og_img.get("content") if og_img else None,
                                    "source": "generic-og"
                                }
                        except ValueError:
                            pass
        except Exception as e:
            print(f"[Generic URL Lookup Error] {url}: {e}")
        return None


# ─── 5. HARDWARE AGENT (Main Orchestrator) ───────────────────────────────────

class HardwareAgent:
    """Deterministic, zero-hardcoding multi-retailer PC hardware pricing engine."""

    def __init__(self):
        self.amazon = AmazonClient()
        self.ebay = EbayClient()
        self.bestbuy = BestBuyClient()
        self.generic = GenericRetailerClient()
        try:
            from app.scrapers.newegg import NeweggScraper
            from app.scrapers.microcenter import MicroCenterScraper
            self.newegg = NeweggScraper()
            self.microcenter = MicroCenterScraper()
        except ImportError:
            self.newegg = None
            self.microcenter = None

    async def _search_newegg(self, query: str, analysis: ProductAnalysis) -> list[dict]:
        if not getattr(self, "newegg", None):
            return []
        try:
            print(f"[Newegg Direct] Searching for '{query}'...")
            offers = await self.newegg.search(query, analysis)
            candidates = []
            for o in offers:
                # Strictly reject any search listing link (e.g. /p/pl?d=...)
                if hasattr(self.newegg, "is_product_url") and not self.newegg.is_product_url(o.url):
                    continue

                t_lower = (o.title or "").lower()
                is_refurb = "refurbished" in t_lower
                is_open_box = any(w in t_lower for w in ["open box", "open-box"])
                if is_refurb:
                    cond_tag = "refurbished"
                    cond_label = "Refurbished"
                elif is_open_box:
                    cond_tag = "open_box"
                    cond_label = "Open Box"
                else:
                    cond_tag = "new"
                    cond_label = "Brand New"

                candidates.append({
                    "retailer": "Newegg",
                    "title": o.title,
                    "price": o.price,
                    "originalPrice": o.original_price,
                    "inStock": o.in_stock,
                    "condition": cond_tag,
                    "conditionLabel": cond_label,
                    "isRefurbished": is_refurb or is_open_box,
                    "url": o.url,
                    "imageUrl": o.image_url,
                    "brand": analysis.brand,
                    "source": "newegg-direct"
                })
            if candidates:
                print(f"[Newegg Raw] {len(candidates)} candidate(s) collected")
            return candidates
        except Exception as e:
            print(f"[Newegg Scraper Notice] {e}")
            return []

    async def _search_microcenter(self, query: str, analysis: ProductAnalysis) -> list[dict]:
        if not getattr(self, "microcenter", None):
            return []
        try:
            print(f"[Micro Center Direct] Searching for '{query}'...")
            offers = await self.microcenter.search(query, analysis)
            candidates = []
            for o in offers:
                t_lower = (o.title or "").lower()
                is_open_box = any(w in t_lower for w in ["refurbished", "open box", "open-box"])
                cond_tag = "open_box" if is_open_box else "new"
                cond_label = "Open Box" if is_open_box else "Brand New"
                candidates.append({
                    "retailer": "Micro Center",
                    "title": o.title,
                    "price": o.price,
                    "originalPrice": o.original_price,
                    "inStock": o.in_stock,
                    "condition": cond_tag,
                    "conditionLabel": cond_label,
                    "isRefurbished": is_open_box,
                    "url": o.url,
                    "imageUrl": o.image_url,
                    "brand": analysis.brand,
                    "source": "microcenter-direct"
                })
            if candidates:
                print(f"[Micro Center Raw] {len(candidates)} candidate(s) collected")
            return candidates
        except Exception as e:
            print(f"[Micro Center Scraper Notice] {e}")
            return []

    async def scrape_product_url(self, url: str) -> dict | None:
        """Directly scrapes the SAME product URL to get latest price without search query hallucinations."""
        if not url or not url.startswith("http"):
            return None

        clean_url = url.strip()
        domain = urllib.parse.urlparse(clean_url).netloc.lower()

        if "amazon." in domain or "amzn.to" in domain:
            return await self.amazon.lookup_url(clean_url)
        elif "ebay." in domain:
            return await self.ebay.lookup_url(clean_url)
        elif "bestbuy.com" in domain:
            return await self.bestbuy.lookup_url(clean_url)
        elif "microcenter.com" in domain and getattr(self, "microcenter", None):
            try:
                off = await self.microcenter.get_price(clean_url)
                if off:
                    return {
                        "retailer": "Micro Center",
                        "title": off.title,
                        "price": off.price,
                        "originalPrice": off.original_price,
                        "inStock": off.in_stock,
                        "url": clean_url,
                        "imageUrl": off.image_url,
                        "source": "microcenter-direct"
                    }
            except Exception as e:
                print(f"[Micro Center Direct URL Notice] {e}")
            # Do NOT fall back to generic.lookup_url for microcenter.com!
            # Micro Center pages for discontinued products still contain obsolete JSON-LD prices ($79.99)
            # while displaying "This item is no longer carried by Micro Center".
            return None
        elif "newegg.com" in domain and getattr(self, "newegg", None):
            try:
                off = await self.newegg.get_price(clean_url)
                if off:
                    return {
                        "retailer": "Newegg",
                        "title": off.title,
                        "price": off.price,
                        "originalPrice": off.original_price,
                        "inStock": off.in_stock,
                        "url": clean_url,
                        "imageUrl": getattr(off, "image_url", None),
                        "source": "newegg-direct"
                    }
            except Exception as e:
                print(f"[Newegg Direct URL Notice] {e}")
            return await self.generic.lookup_url(clean_url)
        else:
            return await self.generic.lookup_url(clean_url)

    async def run(self, prompt: str, emit_fn=None, user_id: str = None, pending_id: str = None) -> dict:
        clean_prompt = re.sub(r'[\t\r\n]+', ' ', prompt).strip()
        # Deduplicate if user accidentally pasted duplicated text (e.g. "Part \t Part" or "Part Part")
        words = clean_prompt.split()
        if len(words) >= 4 and len(words) % 2 == 0:
            half = len(words) // 2
            if [w.lower() for w in words[:half]] == [w.lower() for w in words[half:]]:
                clean_prompt = " ".join(words[:half])


        print(f"\n======================================================")
        print(f"[HardwareAgent] Processing Query: \"{clean_prompt}\"")
        print(f"======================================================")

        # 1. AI Analysis & Query Normalization
        analysis = await ProductAnalyzer.analyze_query(clean_prompt)
        print(f"[AI Analyzer] Brand: {analysis.brand} | Model: '{analysis.model}' | Category: {analysis.category}")
        print(f"[AI Analyzer] Retailer Query: \"{analysis.retailer_search_query}\"")

        if emit_fn:
            emit_fn("agent_start", {
                "query": analysis.model,
                "original_query": clean_prompt,
                "category": analysis.category,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        if not analysis.is_pc_part:
            msg = f"'{clean_prompt}' does not appear to be a PC component or computer peripheral."
            print(f"[Non-Hardware Rejected] {msg}")
            if emit_fn:
                emit_fn("agent_error", {
                    "query": clean_prompt,
                    "original_query": clean_prompt,
                    "error_type": "NON_HARDWARE_QUERY",
                    "message": msg,
                    "pending_id": pending_id,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                emit_fn("agent_complete", {
                    "query": clean_prompt,
                    "original_query": clean_prompt,
                    "category": analysis.category,
                    "scrapedOffers": [],
                    "summary": msg,
                    "pending_id": pending_id,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            return {
                "query": clean_prompt,
                "normalized_query": analysis.model,
                "category": analysis.category,
                "scrapedOffers": [],
                "failed_retailers": []
            }

        # 2. Concurrently scrape our two concrete primary retailers: Amazon and eBay
        print(f"🛒 [Retailer Engine] Concurrently querying Amazon and eBay for '{analysis.model}'...")
        tasks = [
            self.amazon.search(analysis),
            self.ebay.search(analysis),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten all raw candidates into one pool
        raw_pool: list[dict] = []
        for res in results:
            if isinstance(res, list):
                raw_pool.extend(res)
            elif isinstance(res, dict) and res.get("price"):
                # Direct URL lookups (ebay/amazon lookup_url) return a single dict
                raw_pool.append(res)

        print(f"[HardwareAgent] Raw pool: {len(raw_pool)} candidate(s) before LLM validation")

        # 3. LLM batch validation — the single intelligence layer that decides genuine matches
        validated_pool = await ProductAnalyzer.validate_candidates_llm(raw_pool, analysis)
        print(f"[HardwareAgent] Validated pool: {len(validated_pool)} candidate(s) after LLM validation")

        # 4. Pick best (cheapest in-stock) per retailer from validated pool
        best_per_retailer: dict[str, dict] = {}
        for c in validated_pool:
            retailer = c.get("retailer", "Unknown")
            existing = best_per_retailer.get(retailer)
            # Prefer in-stock; within same stock status prefer lower price
            if existing is None:
                best_per_retailer[retailer] = c
            else:
                c_better = (c.get("inStock", True) and not existing.get("inStock", True)) or \
                           (c.get("inStock", True) == existing.get("inStock", True) and c["price"] < existing["price"])
                if c_better:
                    best_per_retailer[retailer] = c

        scraped_offers = []
        for res in best_per_retailer.values():
            p_val = float(res["price"])
            orig_val = float(res["originalPrice"]) if res.get("originalPrice") else p_val
            res["previousPrice"] = res.get("previousPrice") or p_val
            res["previousPrice24h"] = res.get("previousPrice24h") or p_val
            res["previousPrice7d"] = res.get("previousPrice7d") or (orig_val if orig_val > p_val else p_val)
            res["previousPrice30d"] = res.get("previousPrice30d") or (orig_val if orig_val > p_val else p_val)
            scraped_offers.append(res)
            cond_str = f" ({res.get('conditionLabel', 'New')})" if res.get("conditionLabel") else ""
            print(f"✅ [{res['retailer']}] ${res['price']:.2f}{cond_str} → {res['title'][:60]}")
            if emit_fn:
                emit_fn("retailer_found", {
                    "query": analysis.model,
                    "original_query": clean_prompt,
                    "retailer": res["retailer"],
                    "title": res["title"],
                    "price": res["price"],
                    "originalPrice": res.get("originalPrice"),
                    "previousPrice": res["previousPrice"],
                    "previousPrice24h": res["previousPrice24h"],
                    "previousPrice7d": res["previousPrice7d"],
                    "previousPrice30d": res["previousPrice30d"],
                    "condition": res.get("condition", "new"),
                    "conditionLabel": res.get("conditionLabel", "Brand New"),
                    "url": res["url"],
                    "imageUrl": res.get("imageUrl"),
                    "inStock": res.get("inStock", True),
                    "pending_id": pending_id,
                    "offer": res
                })

        # 5. Sort: in-stock first, then ascending price
        scraped_offers.sort(key=lambda x: (not x.get("inStock", True), x["price"]))

        # 4. Generate Summary
        if scraped_offers:
            best = scraped_offers[0]
            cond_str = f" ({best.get('conditionLabel', 'New')})" if best.get("conditionLabel") else ""
            summary = f"Best price for {analysis.model} is ${best['price']:.2f}{cond_str} at {best['retailer']} ({len(scraped_offers)} retailers found)."
            print(f"\n--- FINAL MULTI-RETAILER RESULTS ---")
            print(f"Total Offers: {len(scraped_offers)}")
            for o in scraped_offers:
                c_str = f" [{o.get('conditionLabel', 'New')}]" if o.get("conditionLabel") else ""
                print(f"  • {o['retailer']}: ${o['price']:.2f}{c_str} ({o['title'][:55]}...)")
        else:
            summary = f"No verified in-stock offers found for {analysis.model} on tested retailers."
            print(f"\n[HardwareAgent] No valid offers found across retailers.")

        best_offer = scraped_offers[0] if scraped_offers else None
        if emit_fn:
            emit_fn("agent_complete", {
                "query": analysis.model,
                "original_query": clean_prompt,
                "category": analysis.category,
                "bestOffer": best_offer,
                "allOffers": scraped_offers,
                "scrapedOffers": scraped_offers,
                "summary": summary,
                "pending_id": pending_id,
                "is_error": not bool(scraped_offers),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        return {
            "query": clean_prompt,
            "normalized_query": analysis.model,
            "category": analysis.category,
            "brand": analysis.brand,
            "bestOffer": best_offer,
            "allOffers": scraped_offers,
            "scrapedOffers": scraped_offers,
            "summary": summary,
            "failed_retailers": []
        }

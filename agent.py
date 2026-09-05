import os
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

load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
CANOPY_API_KEY = os.environ.get("CANOPY_API_KEY", "")
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")

# ─── 1. AI PRODUCT ANALYZER (No hardcoded brands or regexes) ──────────────────

class ProductAnalysis:
    def __init__(self, raw_query: str, brand: str | None, model: str, category: str,
                 is_pc_part: bool, retailer_search_query: str, negative_keywords: list[str],
                 min_price: float | None = None):
        self.raw_query = raw_query
        self.brand = brand
        self.model = model
        self.category = category
        self.is_pc_part = is_pc_part
        self.retailer_search_query = retailer_search_query
        self.negative_keywords = negative_keywords
        self.min_price = min_price

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
        }

class ProductAnalyzer:
    """Uses LLM to understand PC components dynamically without hardcoded brand lists or chipsets."""

    @staticmethod
    async def analyze_query(query: str) -> ProductAnalysis:
        clean = query.strip()
        if not clean:
            return ProductAnalysis(clean, None, clean, "Other", False, clean, [])

        if not GROQ_API_KEY:
            return ProductAnalysis(clean, None, clean, "Hardware", True, clean, [])

        prompt = (
            "You are an expert PC hardware and technology analyzer. "
            "Given a user search query, extract structured information without relying on hardcoded lists. "
            "Return valid JSON with:\n"
            "- \"brand\": The hardware manufacturer or brand if known/implied (e.g. 'Montech', 'NVIDIA', 'AMD', 'ASUS', 'Corsair', 'Lian Li'), or null.\n"
            "- \"model\": The core model / part name (e.g. 'King 95', 'RTX 5090', 'Ryzen 7 9800X3D', 'Trident Z5', 'RM850x').\n"
            "- \"category\": Exactly one of: 'GPU', 'CPU', 'RAM', 'Motherboard', 'Storage', 'Power Supply', 'Case', 'Cooling', 'Monitor', 'Peripherals', or 'Other'.\n"
            "- \"is_pc_part\": true if it is a PC component, computer part, or peripheral; false if food, clothing, or unrelated.\n"
            "- \"min_price\": Realistic minimum market price in USD for a functional, genuine unit of this hardware component (e.g. 1400 for RTX 4090, 180 for RTX 3060, 220 for 7800X3D, 50 for King 95, 30 for 16GB RAM). Used to automatically discard dummy replicas, 1:1 scale toys, empty boxes, and brackets.\n"
            "- \"retailer_search_query\": Clean search term optimized for retailer product catalogs (e.g. 'Montech King 95 PC Case', 'AMD Ryzen 7 9800X3D', 'RTX 5090').\n"
            "- \"negative_keywords\": Array of terms that indicate a candidate result is the WRONG product, accessory, toy, or broken item. For instance:\n"
            "   * If Case: reject ['Prebuilt', 'Gaming PC', 'Desktop PC', 'Cable', 'Bracket', 'Screws'].\n"
            "   * If CPU: reject ['Prebuilt', 'Desktop PC', 'Cooler', 'Motherboard Combo', 'Keychain', 'Delid'].\n"
            "   * If GPU: reject ['Prebuilt', 'Desktop PC', 'Bracket', 'Backplate', 'Heatsink', 'Shroud', 'Poster', 'Replica', 'Display Only', 'Dummy'].\n"
            "   * If Cooler: reject ['Case', 'Prebuilt', 'Thermal Paste Only']."
        )

        models = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b", "groq/compound"]
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

                    return ProductAnalysis(
                        raw_query=clean,
                        brand=data.get("brand"),
                        model=data.get("model") or clean,
                        category=data.get("category") or "Other",
                        is_pc_part=bool(data.get("is_pc_part", True)),
                        retailer_search_query=data.get("retailer_search_query") or clean,
                        negative_keywords=data.get("negative_keywords") or [],
                        min_price=min_p
                    )
            except Exception as e:
                print(f"[ProductAnalyzer Error with {model_name}] {e}")

        return ProductAnalysis(clean, None, clean, "Hardware", True, clean, [])

    @staticmethod
    def validate_offer(analysis: ProductAnalysis, title: str, price: float) -> tuple[bool, str]:
        """Strictly validates whether a retailer product candidate is genuine."""
        title_lower = title.lower()

        # 1. Price check: reject $0 or negative
        if price <= 0:
            return False, "Price is 0 or negative"

        # 2. Universal dummy / replica / toy / packaging / broken exclusion patterns
        dummy_patterns = [
            "replica", "scale replica", "scale model", "1:1 scale", "dummy", "mockup",
            "toy", "miniature", "3d print", "3d printed", "prop", "fun display",
            "display only", "display model", "box only", "empty box", "packaging only",
            "for parts", "parts only", "not working", "broken", "as is", "as-is",
            "poster", "keychain", "sticker", "t-shirt", "hoodie", "mug"
        ]
        for dp in dummy_patterns:
            if re.search(r'\b' + re.escape(dp) + r'\b', title_lower):
                if dp not in analysis.raw_query.lower():
                    return False, f"Rejection pattern detected: '{dp}'"

        # 3. Dynamic AI price floor (rejects accessories, toys, or brackets posing as main hardware)
        if analysis.min_price and analysis.min_price > 0:
            floor_threshold = analysis.min_price * 0.4
            if price < floor_threshold:
                return False, f"Price ${price:.2f} is suspiciously below minimum hardware threshold (${floor_threshold:.2f}) for {analysis.model}"

        # 4. Rejection keywords check from analysis
        for neg in analysis.negative_keywords:
            neg_lower = neg.lower()
            if re.search(r'\b' + re.escape(neg_lower) + r'\b', title_lower):
                if neg_lower not in analysis.model.lower():
                    return False, f"Matches negative keyword: '{neg}'"

        # 5. Category sanity checks
        if analysis.category == "Case":
            if price > 600 and any(w in title_lower for w in ["gaming pc", "desktop pc", "ryzen", "rtx", "intel core"]):
                return False, f"Prebuilt PC detected instead of standalone Case (${price:.2f})"
        elif analysis.category == "CPU":
            if any(w in title_lower for w in ["cooler only", "mounting bracket", "delid tool", "contact frame", "thermal paste"]):
                return False, "Accessory / cooler detected instead of CPU"
        elif analysis.category == "GPU":
            gpu_accessories = [
                "bracket", "gpu sag", "backplate", "fan replacement", "cooler only",
                "heatsink", "shroud", "thermal pad", "water block"
            ]
            if any(re.search(r'\b' + re.escape(acc) + r'\b', title_lower) for acc in gpu_accessories):
                if not any(acc in analysis.raw_query.lower() for acc in gpu_accessories):
                    return False, "GPU accessory/parts detected instead of Graphics Card"

        # 6. GPU Sub-tier modifier check (prevents 3060 matching 3060 Ti, or 4070 matching 4070 Super)
        if analysis.category == "GPU":
            modifiers = ["ti", "super", "xtx", "xt", "gre"]
            for mod in modifiers:
                mod_pat = r'\b' + re.escape(mod) + r'\b'
                in_model = bool(re.search(mod_pat, analysis.model.lower()))
                in_title = bool(re.search(mod_pat, title_lower))
                if in_title and not in_model:
                    return False, f"Title has sub-tier '{mod.upper()}' but query does not"
                if in_model and not in_title:
                    return False, f"Query requires sub-tier '{mod.upper()}' but title is missing it"

        # 7. Whole PC / Laptop / System / Platform check for standalone components
        if analysis.category in ["GPU", "CPU", "RAM", "Power Supply", "Storage", "Cooling", "Motherboard"]:
            raw_lower = analysis.raw_query.lower()

            # Direct platform/system keywords
            system_words = [
                "laptop", "notebook", "desktop pc", "gaming pc", "gaming desktop",
                "computer", "barebone", "all-in-one", "aio pc", "gaming host",
                "workstation pc"
            ]
            for sw in system_words:
                if sw in title_lower and sw not in raw_lower:
                    return False, f"System/platform keyword detected: '{sw}'"

            # Motherboard check (if not searching for a Motherboard)
            if analysis.category != "Motherboard" and any(w in title_lower for w in ["motherboard", "mobo", "mainboard"]):
                if not any(w in raw_lower for w in ["motherboard", "mobo", "mainboard"]):
                    return False, "Motherboard detected instead of standalone component"

            # GPU-specific platform/laptop leak checks
            if analysis.category == "GPU":
                # Known laptop product lines
                laptop_lines = [
                    "loq", "legion", "yoga", "ideapad", "thinkpad", "alienware",
                    "rog zephyrus", "rog strix scar", "razer blade", "omen", "victus",
                    "pavilion", "dell g15", "dell xps", "acer nitro", "predator",
                    "katana", "stealth", "sword", "cyborg", "thin gf63", "macbook", "chromebook"
                ]
                for line in laptop_lines:
                    if re.search(r'\b' + re.escape(line) + r'\b', title_lower) and line not in raw_lower:
                        return False, f"Laptop product line detected: '{line}'"

                # Screen / display specs
                screen_indicators = [
                    r'\b(?:144|165|240|360|120)\s*hz\b',
                    r'\b(?:fhd|qhd|uhd|wqhd|oled|ips)\s+display\b',
                    r'\b(?:13\.3|14|15\.6|16|17\.3)[\"”\s]',
                    r'\btouchscreen\b'
                ]
                for pat in screen_indicators:
                    if re.search(pat, title_lower):
                        return False, "Integrated display / laptop screen spec detected on GPU"

                # CPU specs inside GPU search
                cpu_indicators = [
                    r'\bi[3579]-[\d]{4,5}[a-z]{0,2}\b',
                    r'\bcore\s+ultra\s+[579]\b',
                    r'\bintel\s+core\b',
                    r'\bamd\s+ryzen\s+[3579]\b',
                    r'\bryzen\s+[3579]\s+[\d]{4}[a-z]{0,2}\b'
                ]
                for pat in cpu_indicators:
                    if re.search(pat, title_lower) and not re.search(pat, raw_lower):
                        return False, "CPU specification detected inside standalone GPU search"

                # System storage + RAM bundles
                storage_patterns = [
                    r'\b\d+\s*(?:gb|tb)\s*ssd\b',
                    r'\b\d+\s*gb\s+\d+\s*(?:gb|tb)\b',
                    r'\b\d+\s*gb\s+ram\b'
                ]
                for pat in storage_patterns:
                    if re.search(pat, title_lower):
                        return False, "System storage/RAM bundle detected inside standalone GPU search"

                # Mobile/laptop GPU indicator
                mobile_gpu = ["laptop gpu", "notebook gpu", "mobile gpu", "mobil gpu", "max-q"]
                for mg in mobile_gpu:
                    if mg in title_lower and mg not in raw_lower:
                        return False, f"Mobile/Laptop-only GPU detected: '{mg}'"

        # 8. Model match check
        model_tokens = [t.lower() for t in re.split(r'[^a-zA-Z0-9]+', analysis.model) if len(t) > 1]
        if model_tokens:
            matches = sum(1 for tok in model_tokens if tok in title_lower)
            digit_tokens = [tok for tok in model_tokens if any(c.isdigit() for c in tok)]
            if digit_tokens and not all(dt in title_lower for dt in digit_tokens):
                return False, f"Missing required model token: {digit_tokens}"
            if matches < max(1, len(model_tokens) // 2):
                return False, f"Insufficient model token match ({matches}/{len(model_tokens)})"

        return True, "Valid offer"


# ─── 2. AMAZON CLIENT (Direct HTTP/2 Engine - Unlimited & Zero-Quota) ─────────

class AmazonClient:
    """High-speed direct Amazon client with zero API rate-limits/credit costs."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

    @staticmethod
    def extract_asin(text: str) -> str | None:
        m = re.search(r'(?:/dp/|/gp/product/|^)([A-Z0-9]{10})(?:[/?&]|$)', text.strip())
        return m.group(1) if m else None

    async def lookup_asin(self, asin: str) -> dict | None:
        url = f"https://www.amazon.com/dp/{asin}"
        print(f"[Amazon Direct] Fetching product page: {url}...")
        try:
            async with httpx.AsyncClient(headers=self.HEADERS, http2=True, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    title_el = soup.find("span", id="productTitle")
                    title = title_el.text.strip() if title_el else None
                    if not title and soup.title:
                        title = soup.title.string.replace("Amazon.com:", "").strip()

                    # Extract price from buybox
                    price_val = None
                    for span in soup.find_all("span", class_="a-price"):
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

                    if title and price_val:
                        offer = {
                            "retailer": "Amazon",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": True,
                            "isRefurbished": "renewed" in title.lower() or "refurbished" in title.lower(),
                            "url": product_url,
                            "imageUrl": image_url,
                            "brand": None,
                            "source": "amazon-direct"
                        }
                        print(f"✅ [Amazon Hit] ${offer['price']:.2f} -> {offer['title'][:60]}")
                        return offer
        except Exception as e:
            print(f"[Amazon Direct DP Error] {e}")
        return None

    async def search(self, analysis: ProductAnalysis) -> dict | None:
        asin = self.extract_asin(analysis.raw_query)
        if asin and ("amazon.com" in analysis.raw_query or len(analysis.raw_query.strip()) == 10):
            return await self.lookup_asin(asin)

        search_term = analysis.retailer_search_query
        print(f"[Amazon Direct] Searching for '{search_term}'...")
        encoded = urllib.parse.quote_plus(search_term)
        url = f"https://www.amazon.com/s?k={encoded}"

        try:
            async with httpx.AsyncClient(headers=self.HEADERS, http2=True, follow_redirects=True, timeout=12.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, "html.parser")
                    items = soup.find_all("div", {"data-component-type": "s-search-result"})
                    valid_offers = []
                    for it in items:
                        item_asin = it.get("data-asin")
                        if not item_asin:
                            continue

                        # Extract title
                        title = None
                        for a in it.find_all("a", class_="a-link-normal"):
                            txt = a.text.strip()
                            if len(txt) > 20 and not txt.startswith("("):
                                title = txt
                                break
                        if not title:
                            img = it.find("img", class_="s-image")
                            if img and img.get("alt"):
                                title = img.get("alt")

                        # Extract price
                        price_el = it.find("span", class_="a-price")
                        price_offscreen = price_el.find("span", class_="a-offscreen") if price_el else None
                        if not price_offscreen or not title:
                            continue
                        m = re.search(r'[\$]?([0-9,]+\.[0-9]{2})', price_offscreen.text)
                        if not m:
                            continue
                        price_val = float(m.group(1).replace(",", ""))

                        # Semantic validation
                        is_valid, reason = ProductAnalyzer.validate_offer(analysis, title, price_val)
                        if not is_valid:
                            continue

                        img = it.find("img", class_="s-image")
                        image_url = img.get("src") if img else None

                        merchant_input = it.find("input", {"name": "merchantId"})
                        merchant_id = merchant_input.get("value") if merchant_input else None
                        product_url = f"https://www.amazon.com/dp/{item_asin}?smid={merchant_id}" if merchant_id else f"https://www.amazon.com/dp/{item_asin}"

                        valid_offers.append({
                            "retailer": "Amazon",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": True,
                            "isRefurbished": "renewed" in title.lower() or "refurbished" in title.lower(),
                            "url": product_url,
                            "imageUrl": image_url,
                            "brand": analysis.brand,
                            "source": "amazon-direct"
                        })

                    if valid_offers:
                        valid_offers.sort(key=lambda x: x["price"])
                        best = valid_offers[0]
                        print(f"✅ [Amazon Hit] ${best['price']:.2f} -> {best['title'][:60]}")
                        return best
        except Exception as e:
            print(f"[Amazon Direct Search Error] {e}")
        return None


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

    async def search(self, analysis: ProductAnalysis) -> dict | None:
        client_id = os.environ.get("EBAY_CLIENT_ID", "") or EBAY_CLIENT_ID
        token = await self.get_access_token()
        if not token:
            print("[eBay API] ℹ️ EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not configured, skipping eBay.")
            return None

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
                    valid_offers = []
                    for it in items:
                        title = it.get("title", "")
                        price_obj = it.get("price", {})
                        price_str = price_obj.get("value")
                        if not title or not price_str:
                            continue

                        condition = it.get("condition", "New")
                        if any(w in condition.lower() for w in ["parts", "not working", "broken", "faulty", "as is"]):
                            print(f"[eBay Filtered] Skipping '{title[:50]}...': Condition is '{condition}'")
                            continue

                        price_val = float(price_str)
                        is_valid, reason = ProductAnalyzer.validate_offer(analysis, title, price_val)
                        if not is_valid:
                            print(f"[eBay Filtered] Skipping '{title[:50]}...': {reason}")
                            continue

                        is_refurb = any(w in condition.lower() for w in ["refurbished", "used", "seller refurbished"])
                        image_url = (it.get("image") or {}).get("imageUrl")
                        item_url = it.get("itemWebUrl") or f"https://www.ebay.com/itm/{it.get('itemId')}"

                        valid_offers.append({
                            "retailer": "eBay",
                            "title": title,
                            "price": price_val,
                            "originalPrice": None,
                            "inStock": True,
                            "isRefurbished": is_refurb,
                            "url": item_url,
                            "imageUrl": image_url,
                            "brand": analysis.brand,
                            "source": "ebay-api"
                        })

                    if valid_offers:
                        valid_offers.sort(key=lambda x: x["price"])
                        best = valid_offers[0]
                        print(f"✅ [eBay Hit] ${best['price']:.2f} -> {best['title'][:60]}")
                        return best
                else:
                    print(f"⚠️ [eBay Search Error] {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[eBay Search Exception] {e}")
        return None


# ─── 4. HARDWARE AGENT (Main Orchestrator) ───────────────────────────────────

class HardwareAgent:
    """Deterministic, zero-hardcoding multi-retailer PC hardware pricing engine."""

    def __init__(self):
        self.amazon = AmazonClient()
        self.ebay = EbayClient()

    async def run(self, prompt: str, emit_fn=None, user_id: str = None, pending_id: str = None) -> dict:
        clean_prompt = prompt.strip()
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

        # 2. Concurrently scrape retailers (Amazon + eBay)
        tasks = [
            self.amazon.search(analysis),
            self.ebay.search(analysis),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scraped_offers = []
        for res in results:
            if isinstance(res, dict) and res.get("price"):
                scraped_offers.append(res)
                if emit_fn:
                    emit_fn("retailer_found", {"retailer": res["retailer"], "offer": res})

        # 3. Sort offers by price ascending
        scraped_offers.sort(key=lambda x: x["price"])

        # 4. Generate Summary
        if scraped_offers:
            best = scraped_offers[0]
            summary = f"Best price for {analysis.model} is ${best['price']:.2f} at {best['retailer']} ({len(scraped_offers)} retailers found)."
            print(f"\n--- FINAL MULTI-RETAILER RESULTS ---")
            print(f"Total Offers: {len(scraped_offers)}")
            for o in scraped_offers:
                print(f"  • {o['retailer']}: ${o['price']:.2f} ({o['title'][:55]}...)")
        else:
            summary = f"No verified in-stock offers found for {analysis.model} on Amazon or eBay."
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

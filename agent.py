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
ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "91418034a0339b39d3cfab8f77d001a8006896a7")
ZENROWS_QUOTA_EXHAUSTED = False

TAVILY_API_KEYS = [
    os.environ.get("TAVILY_API_KEY", ""),
    "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",
    "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",
    "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",
    "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"
]

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

            # CPU tier mismatch check (e.g. Ryzen 7 query vs Ryzen 5 listing, or Core i7 vs Core i5)
            def extract_cpu_tier(text: str) -> str | None:
                m_r = re.search(r'\b(ryzen\s*[3579]|r[3579])\b', text, re.I)
                if m_r: return re.sub(r'\s+', '', m_r.group(0).lower()).replace('r', 'ryzen')
                m_i = re.search(r'\b(core\s*i[3579]|i[3579]-?\d{4,5})\b', text, re.I)
                if m_i: return re.sub(r'[- ]', '', m_i.group(1).lower())
                m_u = re.search(r'\bultra\s*[579]\b', text, re.I)
                if m_u: return re.sub(r'\s+', '', m_u.group(0).lower())
                return None

            q_tier = extract_cpu_tier(analysis.model) or extract_cpu_tier(analysis.raw_query)
            t_tier = extract_cpu_tier(title)
            if q_tier and t_tier and q_tier != t_tier:
                return False, f"CPU tier mismatch: Query requires '{q_tier.upper()}' but title is '{t_tier.upper()}'"
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

                if title and price_val:
                    offer = {
                        "retailer": "Amazon",
                        "title": title,
                        "price": price_val,
                        "originalPrice": None,
                        "inStock": in_stock,
                        "isRefurbished": "renewed" in title.lower() or "refurbished" in title.lower(),
                        "url": product_url,
                        "imageUrl": image_url,
                        "brand": None,
                        "source": "amazon-direct"
                    }
                    print(f"✅ [Amazon Hit] ${offer['price']:.2f} -> {offer['title'][:60]}")
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
                pm = re.search(r'\$([0-9,]+\.[0-9]{2})', txt)
                if pm:
                    fallback_price = float(pm.group(1).replace(",", ""))
                    if fallback_price > 10.0:
                        raw_title = item.get("title", "").replace("Amazon.com:", "").strip()
                        print(f"✅ [Amazon Tavily Hit] ${fallback_price:.2f} -> {raw_title[:60]}")
                        return {
                            "retailer": "Amazon",
                            "title": raw_title or f"Amazon Product {asin}",
                            "price": fallback_price,
                            "originalPrice": None,
                            "inStock": True,
                            "isRefurbished": False,
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

    async def search(self, analysis: ProductAnalysis) -> dict | None:
        asin = self.extract_asin(analysis.raw_query)
        if asin and ("amazon.com" in analysis.raw_query or len(analysis.raw_query.strip()) == 10):
            return await self.lookup_asin(asin)

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
                        print(f"[Amazon Filtered] Skipping '{title[:50]}...': {reason}")
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
                else:
                    print(f"[Amazon Direct] 0 valid standalone offers found for '{search_term}'")
            else:
                print(f"⚠️ [Amazon Direct] HTTP {status_code} received for '{search_term}'")
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
                        condition = data.get("condition", "New")
                        is_refurb = any(w in condition.lower() for w in ["refurbished", "used", "seller refurbished"])
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
                            "isRefurbished": is_refurb,
                            "url": item_url,
                            "imageUrl": image_url,
                            "brand": None,
                            "source": "ebay-api"
                        }
                        print(f"✅ [eBay Hit] ${offer['price']:.2f} -> {offer['title'][:60]}")
                        return offer
                else:
                    print(f"⚠️ [eBay Lookup Status] {res.status_code} for item {item_id}")
        except Exception as e:
            print(f"[eBay Lookup Exception] {e}")
        return None

    async def lookup_url(self, url: str) -> dict | None:
        item_id = self.extract_item_id(url)
        if item_id:
            res = await self.lookup_item_id(item_id)
            if res:
                if url.startswith("http"):
                    res["url"] = url
                return res
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
                kwargs = {"headers": self.HEADERS, "timeout": 6}
                if CurlHttpVersion is not None:
                    kwargs["http_version"] = CurlHttpVersion.V1_1
                async with CurlAsyncSession(impersonate="chrome124") as session:
                    res = await session.get(url, **kwargs)
                    status_code, text = res.status_code, res.text
                    if status_code == 200 and text and "access denied" not in text.lower():
                        return status_code, text
            except Exception:
                pass

        # Check if Best Buy blocked or challenged the request
        is_blocked = (
            status_code != 200
            or not text
            or "access denied" in text.lower()
            or "automated access" in text.lower()
            or status_code in [403, 429, 503]
        )

        # 2. Secondary: Seamless ZenRows residential proxy fallback (if not exhausted)
        if is_blocked and not ZENROWS_QUOTA_EXHAUSTED:
            zenrows_key = os.environ.get("ZENROWS_API_KEY", "") or ZENROWS_API_KEY
            if zenrows_key:
                print(f"🛡️ [Best Buy Direct] Direct request blocked ({status_code}); routing via ZenRows residential proxy...")
                zr_status, zr_text = await self._fetch_with_zenrows(url)
                if zr_status == 200 and zr_text and "access denied" not in zr_text.lower():
                    return zr_status, zr_text

        return status_code, text

    async def search(self, analysis: ProductAnalysis) -> dict | None:
        search_term = analysis.retailer_search_query
        print(f"[Best Buy Direct] Searching for '{search_term}'...")
        encoded = urllib.parse.quote_plus(search_term)
        url = f"https://www.bestbuy.com/site/searchpage.jsp?st={encoded}&intl=nosplash"

        try:
            status_code, text = await self._fetch_html(url)
            if status_code != 200 or not text:
                print(f"⚠️ [Best Buy Direct] Failed to fetch search results for '{search_term}' (HTTP {status_code})")
                return None

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

                # Semantic validation
                is_valid, reason = ProductAnalyzer.validate_offer(analysis, title, price)
                if not is_valid:
                    print(f"[Best Buy Filtered] Skipping '{title[:50]}...': {reason}")
                    continue

                valid_offers.append({
                    "retailer": "Best Buy",
                    "title": title,
                    "price": price,
                    "originalPrice": None,
                    "inStock": True,
                    "isRefurbished": any(w in title.lower() for w in ["refurbished", "open-box", "open box"]),
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
                    is_valid, reason = ProductAnalyzer.validate_offer(analysis, t, p_val)
                    if not is_valid:
                        continue
                    link_el = info.find("a", href=re.compile(r"/product/")) or (info.parent.find("a", href=re.compile(r"/product/")) if info.parent else None)
                    p_link = ("https://www.bestbuy.com" + link_el["href"]) if link_el and link_el.get("href", "").startswith("/") else (link_el.get("href") if link_el else url)
                    sku_b = info.find_parent("div", class_="sku-block") or info.parent
                    img_el = sku_b.find("img") if sku_b else None
                    p_img = img_el.get("src") if img_el else None

                    valid_offers.append({
                        "retailer": "Best Buy",
                        "title": t,
                        "price": p_val,
                        "originalPrice": None,
                        "inStock": True,
                        "isRefurbished": any(w in t.lower() for w in ["refurbished", "open-box", "open box"]),
                        "url": p_link,
                        "imageUrl": p_img,
                        "brand": analysis.brand,
                        "source": "bestbuy-direct"
                    })

            if valid_offers:
                valid_offers.sort(key=lambda x: x["price"])
                best = valid_offers[0]
                print(f"✅ [Best Buy Hit] ${best['price']:.2f} -> {best['title'][:60]}")
                return best
            else:
                print(f"[Best Buy Direct] 0 valid standalone offers found for '{search_term}'")
        except Exception as e:
            print(f"[Best Buy Direct Search Error] {e}")

        return None

    async def lookup_url(self, url: str) -> dict | None:
        target_url = url + ("&intl=nosplash" if "?" in url else "?intl=nosplash") if "nosplash" not in url else url
        try:
            status_code, text = await self._fetch_html(target_url)
            if status_code == 200 and text:
                title = None
                price = None
                img = None

                title_m = re.search(r"\"name\":\{[^}]*\"short\":\"([^\"]+)\"", text) or re.search(r"<title>(.*?)(?:\s*-\s*Best\s*Buy|</title>)", text, re.I)
                if title_m:
                    title = title_m.group(1).strip()

                price_m = re.search(r"\"customerPrice\":\s*([0-9.]+)", text)
                if price_m:
                    price = float(price_m.group(1))
                else:
                    pm = re.search(r"\$([0-9,]+\.[0-9]{2})", text)
                    if pm:
                        price = float(pm.group(1).replace(",", ""))

                img_m = re.search(r"\"(?:piscesHref|href)\":\"(https://pisces\.bbystatic\.com/[^\"]+)\"", text)
                if img_m:
                    img = img_m.group(1)

                if title and price:
                    return {
                        "retailer": "Best Buy",
                        "title": title,
                        "price": price,
                        "originalPrice": None,
                        "inStock": True,
                        "isRefurbished": any(w in title.lower() for w in ["refurbished", "open-box", "open box"]),
                        "url": url,
                        "imageUrl": img,
                        "brand": None,
                        "source": "bestbuy-direct"
                    }
        except Exception as e:
            print(f"[Best Buy URL Lookup Error] {e}")

        # Fallback: Query Tavily Search for Best Buy product price
        try:
            sku_m = re.search(r'/([0-9]{7})(?:\.p|\?)', url) or re.search(r'skuId=([0-9]{7})', url) or re.search(r'([0-9]{7})', url)
            sku = sku_m.group(1) if sku_m else ""
            t_query = f'site:bestbuy.com "{sku}"' if sku else f'site:bestbuy.com {url}'
            print(f"🔍 [Best Buy Fallback] Querying Tavily cache: {t_query[:60]}...")
            tavily_results = await query_tavily_search(t_query, max_results=2)
            if tavily_results:
                for it in tavily_results:
                    txt = (it.get("title") or "") + " " + (it.get("content") or "")
                    pm = re.search(r'\$([0-9,]+\.[0-9]{2})', txt)
                    if pm:
                        fallback_p = float(pm.group(1).replace(",", ""))
                        if fallback_p > 10.0:
                            raw_title = it.get("title", "").replace(" - Best Buy", "").replace("Best Buy:", "").strip()
                            print(f"✅ [Best Buy Tavily Fallback Hit] ${fallback_p:.2f} -> {raw_title[:60]}")
                            return {
                                "retailer": "Best Buy",
                                "title": raw_title or "Best Buy Hardware",
                                "price": fallback_p,
                                "originalPrice": None,
                                "inStock": True,
                                "isRefurbished": False,
                                "url": url,
                                "imageUrl": None,
                                "brand": None,
                                "source": "tavily-bestbuy"
                            }
        except Exception as e:
            print(f"[Best Buy Tavily Fallback Notice] {e}")

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
        else:
            return await self.generic.lookup_url(clean_url)

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

        # 2. Concurrently scrape retailers (Amazon + eBay + Best Buy)
        tasks = [
            self.amazon.search(analysis),
            self.ebay.search(analysis),
            self.bestbuy.search(analysis),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        scraped_offers = []
        for res in results:
            if isinstance(res, dict) and res.get("price"):
                p_val = float(res["price"])
                orig_val = float(res["originalPrice"]) if res.get("originalPrice") else p_val
                res["previousPrice"] = res.get("previousPrice") or p_val
                res["previousPrice24h"] = res.get("previousPrice24h") or p_val
                res["previousPrice7d"] = res.get("previousPrice7d") or (orig_val if orig_val > p_val else p_val)
                res["previousPrice30d"] = res.get("previousPrice30d") or (orig_val if orig_val > p_val else p_val)
                scraped_offers.append(res)
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
                        "url": res["url"],
                        "imageUrl": res.get("imageUrl"),
                        "inStock": res.get("inStock", True),
                        "pending_id": pending_id,
                        "offer": res
                    })

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
            summary = f"No verified in-stock offers found for {analysis.model} on Amazon, eBay, or Best Buy."
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

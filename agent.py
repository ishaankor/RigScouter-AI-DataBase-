import os
import json
import time
import asyncio
import requests
import httpx
from datetime import datetime, timezone
import random
import re
from bs4 import BeautifulSoup
from firecrawl import FirecrawlApp

from supabase_client import supabase

TAVILY_API_KEYS = [k.strip() for k in os.environ.get("TAVILY_API_KEYS", os.environ.get("TAVILY_API_KEY", "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")).split(',') if k.strip()]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
FIRECRAWL_API_KEYS = [k.strip() for k in os.environ.get("FIRECRAWL_API_KEYS", os.environ.get("FIRECRAWL_API_KEY", "")).split(',') if k.strip()]
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

firecrawl_apps = [FirecrawlApp(api_key=key) for key in FIRECRAWL_API_KEYS] if FIRECRAWL_API_KEYS else []

def clean_hardware_query(query: str, category: str = None) -> str:
    s = query.strip()
    # Normalize common hardware acronyms and casing
    s = re.sub(r'(?i)\bgeforce\b', 'GeForce', s)
    s = re.sub(r'(?i)\bradeon\b', 'Radeon', s)
    s = re.sub(r'(?i)\bwi-?fi\b', 'WiFi', s)
    s = re.sub(r'(?i)\bmini-?itx\b', 'ITX', s)
    s = re.sub(r'(?i)\bmicro-?atx\b', 'mATX', s)
    s = re.sub(r'(?i)\bgen\s*([345])\b', r'Gen\1', s)
    
    # Split glued tokens (e.g. Z890WiFi -> Z890 WiFi, RTX4070 -> RTX 4070, Ryzen7 -> Ryzen 7)
    s = re.sub(r'(?i)(z\d{3}|b\d{3}|x\d{3}|a\d{3})(wifi|pro|plus|gaming|elite|hero|taichi|max)', r'\1 \2', s)
    s = re.sub(r'(?i)(rtx|gtx|rx)(\d{3,4})', r'\1 \2', s)
    s = re.sub(r'(?i)(ryzen|core)(\d+)', r'\1 \2', s)
    s = re.sub(r'(\d+)(gb|tb|w|mhz|ghz)\b', r'\1\2', s, flags=re.I)
    
    # Strip marketing filler while preserving all model, brand, capacity, and spec tokens
    FLUFF_PATTERNS = [
        r'(?i)\b(?:desktop\s+processor|boxed\s+processor|unlocked\s+desktop\s+processor)\b',
        r'(?i)\b(?:graphics\s+card|video\s+card|gaming\s+graphics\s+card)\b',
        r'(?i)\b(?:gaming\s+motherboard|desktop\s+motherboard)\b',
        r'(?i)\b(?:internal\s+solid\s+state\s+drive|solid\s+state\s+drive|internal\s+ssd)\b',
        r'(?i)\b(?:desktop\s+memory|pc\s+gaming\s+memory|dram\s+desktop\s+memory)\b',
        r'(?i)\b(?:power\s+supply\s+unit)\b',
        r'(?i)\b(?:heatsink\s+not\s+included|cooler\s+not\s+included|no\s+cooler|without\s+cooler)\b',
        r'(?i)\b(?:brand\s+new|sealed\s+in\s+box|factory\s+sealed)\b',
    ]
    for pattern in FLUFF_PATTERNS:
        s = re.sub(pattern, ' ', s)
        
    s = re.sub(r'\s+', ' ', s).strip()
    return s

class TavilyHardwareAgent:
    def __init__(self):
        self.current_key_index = 0
        self.current_firecrawl_index = 0

    def get_tavily_key(self):
        return TAVILY_API_KEYS[self.current_key_index % len(TAVILY_API_KEYS)]

    def rotate_tavily_key(self):
        if len(TAVILY_API_KEYS) > 1:
            self.current_key_index = (self.current_key_index + 1) % len(TAVILY_API_KEYS)
            print(f"[Tavily Key Rotated] Active key index: {self.current_key_index + 1}/{len(TAVILY_API_KEYS)}")

    def get_firecrawl_app(self):
        if not firecrawl_apps:
            return None
        return firecrawl_apps[self.current_firecrawl_index % len(firecrawl_apps)]

    def rotate_firecrawl_key(self):
        if len(firecrawl_apps) > 1:
            self.current_firecrawl_index = (self.current_firecrawl_index + 1) % len(firecrawl_apps)
            print(f"[Firecrawl Key Rotated] Active key index: {self.current_firecrawl_index + 1}/{len(firecrawl_apps)}")

    # --------------------------------------------------------
    # FAST REGEX PRE-FILTER
    # Classifies all known hardware categories instantly.
    # --------------------------------------------------------
    _REGEX_RULES = [
        # GPU — RTX / GTX / Titan / Quadro (NVIDIA)
        (re.compile(r'\b(geforce\s+)?(rtx|gtx)\s*\d{3,4}(?:\s*(super|ti|xt))?\b', re.I), 'GPU'),
        # GPU — Radeon RX (AMD)
        (re.compile(r'\b(radeon\s+)?rx\s*\d{3,4}(?:\s*(xt|xtx|gre))?\b', re.I), 'GPU'),
        # GPU — Intel Arc
        (re.compile(r'\barc\s+[ab]\d{3,4}\b', re.I), 'GPU'),
        # GPU — RX 9xxx / 8xxx
        (re.compile(r'\brx\s*[89]\d{3}(?:\s*(xt|xtx))?\b', re.I), 'GPU'),
        # CPU — AMD Ryzen & Threadripper
        (re.compile(r'\b(?:amd\s+)?(ryzen\s*[3579]|threadripper)\s+\d{4,5}[a-z0-9]*(?:\s*x3d)?\b', re.I), 'CPU'),
        # CPU — Intel Core Ultra
        (re.compile(r'\b(?:intel\s+)?(?:core\s+)?ultra\s*[3579]\s*(?:series\s*2\s*)?[- ]?\d{3,4}[a-z]*(?:\s+plus)?\b', re.I), 'CPU'),
        (re.compile(r'\b(?:intel\s+)?(?:core\s+)?(ultra\s*[3579]|i[3579])[- ]\d{3,6}[a-z]*(?:\s+plus)?\b', re.I), 'CPU'),
        # CPU — Intel Core i-series
        (re.compile(r'\b(?:intel\s+)?(?:core\s+)?i[3579][ -]\d{4,5}[a-z]*\b', re.I), 'CPU'),
        # Motherboard — brand + chipset
        (re.compile(r'\b(asrock|asus|msi|gigabyte|nzxt|biostar)\s+(z|b|x|a)\d{3}[a-z]*(?:\s+(?:wifi|pro|plus|gaming|elite|hero|taichi|extreme|max|tomahawk|aorus|strix|tuf))?\b', re.I), 'Motherboard'),
        # Motherboard — chipset + form factor
        (re.compile(r'\b(z\d{3}|b\d{3}|x\d{3}|a\d{3})\s*(e|f|p|m|a|plus|pro|max|wifi|gaming)?\s*(motherboard|atx|matx|itx|mainboard)?\b', re.I), 'Motherboard'),
        # RAM — DDR capacity/speed / famous series
        (re.compile(r'\b(trident\s*z\d?|dominator|vengeance|ripjaws|fury\s+beast|t-force|g\.?skill)\b', re.I), 'RAM'),
        (re.compile(r'\b\d+gb\s+(?:kit\s+)?(?:\(\d+x\d+gb\)\s+)?(ddr[45]|so-?dimm)\b', re.I), 'RAM'),
        (re.compile(r'\bddr[45][- ]\d{4,5}\b', re.I), 'RAM'),
        (re.compile(r'\bddr[45]\s+\d+gb\b', re.I), 'RAM'),
        # Storage — SSD / NVMe / HDD / famous models
        (re.compile(r'\b(990\s+pro|980\s+pro|sn850x|sn770|t700|t500|kc3000|firecuda|ironwolf|barracuda)\b', re.I), 'Storage'),
        (re.compile(r'\b\d+(?:\.\d+)?\s*(tb|gb)\s+(nvme|ssd|hdd|m\.2|solid\s+state|gen[45])\b', re.I), 'Storage'),
        (re.compile(r'\b(nvme|ssd|m\.2)\s+\d+(?:\.\d+)?\s*(tb|gb)\b', re.I), 'Storage'),
        # Power Supply — PSU models and wattages
        (re.compile(r'\b(rm\d{3,4}[a-z]*|sf\d{3,4}|focus\s+gx|toughpower|dark\s+power|pure\s+power|supernova)\b', re.I), 'Power Supply'),
        (re.compile(r'\b\d{3,4}\s*w\s+(psu|power\s+supply|modular|atx|sfx)\b', re.I), 'Power Supply'),
        (re.compile(r'\b(psu|power\s+supply)\s+\d{3,4}\s*w\b', re.I), 'Power Supply'),
        # Cooling — AIO / Air Coolers / Fans / Paste
        (re.compile(r'\b(nh-d15|nh-u12a|peerless\s+assassin|phantom\s+spirit|ak620|lt720|kraken|liquid\s+freezer|galahad|icue\s+link|kryonaut|mx-[456]|nt-h[12]|uni\s+fan)\b', re.I), 'Cooling'),
        (re.compile(r'\b(aio\s+liquid|liquid\s+cooler|cpu\s+cooler|air\s+cooler|thermal\s+paste|case\s+fan|case\s+fans|120mm\s+fan|140mm\s+fan)\b', re.I), 'Cooling'),
        # Case — popular cases / form factors
        (re.compile(r'\b(o11\s+dynamic|lancool|h[5679]\s+flow|fractal\s+north|meshify|pop\s+air|4000d|5000d|king\s+95|nv[57]|y[67]0)\b', re.I), 'Case'),
        (re.compile(r'\b(pc\s+case|mid\s+tower|full\s+tower|mini\s+itx\s+case|atx\s+case|dual\s+chamber)\b', re.I), 'Case'),
        # Monitor — specs & models
        (re.compile(r'\b(ultragear|odyssey\s+g\d|alienware\s+aw\d{4}|rog\s+swift)\b', re.I), 'Monitor'),
        (re.compile(r'\b\d{2,3}\s*(hz|inch\s+monitor|ips|oled|va|tn)\b', re.I), 'Monitor'),
        (re.compile(r'\b\d{2,3}(?:\.\d)?["\u201d]?\s*(4k|1440p|1080p|ips|oled|va|tn)\s*(monitor|display|screen)?\b', re.I), 'Monitor'),
        # Peripherals — Keyboards, Mice, Headsets, Mics, Stream Decks
        (re.compile(r'\b(superlight|viper\s+v\d|deathadder|basilisk|g502|wooting|keychron|apex\s+pro|huntsman|stream\s+deck|wave:?\s*3|shure\s+sm7|blackshark)\b', re.I), 'Peripherals'),
        (re.compile(r'\b(gaming\s+mouse|mechanical\s+keyboard|gaming\s+headset|usb\s+microphone|capture\s+card)\b', re.I), 'Peripherals'),
    ]

    def _fast_classify(self, query: str) -> dict | None:
        """Return classification instantly if the query matches a known hardware pattern."""
        text = query.strip()
        for pattern, category in self._REGEX_RULES:
            if pattern.search(text):
                cleaned = clean_hardware_query(text, category)
                print(f"[Fast Classify] '{text}' → '{cleaned}' / {category} (regex)")
                return {"model": cleaned, "category": category}
        return None

    async def analyze_query_with_groq(self, query: str) -> dict:
        # Try fast pre-filter first
        fast = self._fast_classify(query)
        if fast:
            return fast

        if not GROQ_API_KEY:
            cleaned = clean_hardware_query(query)
            return {"model": cleaned, "category": "Hardware"}

        system_prompt = (
            "You are an expert PC hardware classifier and query normalizer. "
            "Given a user search query for computer hardware or peripherals, extract the canonical component name and classify it into an exact category. "
            "Return ONLY a JSON object with two keys: 'model' and 'category'. "
            "For 'model': Clean marketing fluff (e.g. 'Desktop Processor', 'Graphics Card') while PRESERVING brand, series, sub-model, capacities (e.g. '2TB', '850W', '32GB'), and colors. "
            "Only set 'model' to EXACTLY 'GENERIC_QUERY_ERROR' if the query is a completely broad product family with no specific model number (e.g. 'RTX 40 series', 'Ryzen 5000 series', 'DDR5 RAM'). "
            "For 'category': Classify into ONE of: GPU, CPU, RAM, Motherboard, Storage, Power Supply, Case, Cooling, Monitor, Peripherals, Accessories. "
            "Only if the item is completely unrelated to computers or gaming (e.g. 'iPhone', 'Nike shoes', 'Car tires'), set 'category' to 'Not compatible (N/A)'. "
            "Examples:\n"
            "Input: ASUS ROG Strix GeForce RTX 4090 OC 24GB -> {\"model\": \"ASUS ROG Strix RTX 4090\", \"category\": \"GPU\"}\n"
            "Input: Lian Li O11 Dynamic EVO RGB White -> {\"model\": \"Lian Li O11 Dynamic EVO RGB White\", \"category\": \"Case\"}\n"
            "Input: Noctua NH-D15 chromax.black -> {\"model\": \"Noctua NH-D15 chromax.black\", \"category\": \"Cooling\"}\n"
            "Input: Corsair RM850x 850W Gold PSU -> {\"model\": \"Corsair RM850x 850W\", \"category\": \"Power Supply\"}\n"
            "Input: Samsung 990 Pro 2TB NVMe SSD -> {\"model\": \"Samsung 990 Pro 2TB\", \"category\": \"Storage\"}\n"
            "Input: Wooting 60HE+ Mechanical Keyboard -> {\"model\": \"Wooting 60HE+\", \"category\": \"Peripherals\"}\n"
            "Input: Logitech G Pro X Superlight 2 Wireless -> {\"model\": \"Logitech G Pro X Superlight 2\", \"category\": \"Peripherals\"}\n"
            "Input: Thermal Grizzly Kryonaut 1g -> {\"model\": \"Thermal Grizzly Kryonaut 1g\", \"category\": \"Cooling\"}\n"
            "Input: iPhone 15 Pro Max -> {\"model\": \"iPhone 15 Pro Max\", \"category\": \"Not compatible (N/A)\"}\n"
        )

        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": "groq/compound-mini",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Title: {query}"}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0
                    },
                    timeout=10.0
                )
                if res.status_code == 200:
                    raw_content = res.json()["choices"][0]["message"]["content"]
                    print(f"[DEBUG Groq Analyzer] Raw JSON response: {raw_content}")
                    data = json.loads(raw_content)

                    # Smart context fallback: if it's marked Not compatible, check if web context clarifies it as hardware
                    if data.get('category') == 'Not compatible (N/A)' and self.get_tavily_key():
                        try:
                            print(f"[AI Analyzer] '{query}' marked Not compatible. Fetching web context...")
                            tavily_res = await client.post('https://api.tavily.com/search', json={
                                "api_key": self.get_tavily_key(),
                                "query": f"{query} specs computer hardware",
                                "search_depth": "basic",
                                "max_results": 1
                            }, timeout=3.0)
                            if tavily_res.status_code == 200:
                                results = tavily_res.json().get('results', [])
                                snippet = results[0].get('content', '') if results else ''
                                if snippet:
                                    res2 = await client.post(
                                        "https://api.groq.com/openai/v1/chat/completions",
                                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                                        json={
                                            "model": "groq/compound-mini",
                                            "messages": [
                                                {"role": "system", "content": system_prompt + "\nUse the search context to verify if this is a computer hardware item."},
                                                {"role": "user", "content": f"Title: {query}\n\nSearch Context: {snippet}"}
                                            ],
                                            "response_format": {"type": "json_object"},
                                            "temperature": 0
                                        },
                                        timeout=5.0
                                    )
                                    if res2.status_code == 200:
                                        data = json.loads(res2.json()["choices"][0]["message"]["content"])
                        except Exception as e:
                            print(f"[Tavily Fallback Error] {e}")

                    return data
                else:
                    print(f"[Groq Analyzer] HTTP {res.status_code}: {res.text[:200]}")
        except Exception as e:
            print(f"[Groq Analyzer Error] {e}")

        cleaned = clean_hardware_query(query)
        return {"model": cleaned, "category": "Hardware"}

    async def run(self, prompt: str, emit_fn=None, user_id: str = None, pending_id: str = None) -> dict:
        clean_prompt = prompt.strip()
        is_url = clean_prompt.startswith('http://') or clean_prompt.startswith('https://')
        
        if not is_url:
            print(f"[AI Analyzer] Normalizing and categorizing query: '{clean_prompt}'")
            result = await self.analyze_query_with_groq(clean_prompt)
            clean_prompt = result.get('model', clean_prompt)
            category = result.get('category', 'Not compatible (N/A)')
            print(f"[AI Analyzer] Result: '{clean_prompt}' ({category})")
        else:
            category = await self.detect_category(clean_prompt)

        if emit_fn:
            emit_fn('agent_start', {'query': clean_prompt, 'original_query': prompt.strip(), 'category': category, 'timestamp': datetime.now(timezone.utc).isoformat()})

        state = {
            "userQuery": clean_prompt,
            "category": category,
            "scrapedOffers": [],
            "summary": ""
        }

        if clean_prompt == 'GENERIC_QUERY_ERROR':
            print(f"[Generic Query Rejected] \"{prompt.strip()}\"")
            state["summary"] = f"Your search '{prompt.strip()}' is too broad (e.g. a general chipset or product family). Please search for a specific model (e.g. 'ASUS ROG Strix X870-A') for accurate pricing."
            if emit_fn:
                emit_fn('agent_complete', {
                    "query": prompt.strip(),
                    "original_query": prompt.strip(),
                    "category": category,
                    "bestOffer": None,
                    "allOffers": [],
                    "summary": state["summary"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            return state

        print(f"\n======================================================")
        print(f"[Hybrid Python Agent] Extracting price for: \"{clean_prompt}\" ({category})")
        print(f"======================================================\n")


        if category == 'Not compatible (N/A)' and not is_url:
            print(f"[Non-PC Part Query Rejected] \"{clean_prompt}\" is Not compatible (N/A)")
            state["summary"] = f"Not compatible (N/A) — \"{clean_prompt}\" is not a recognized PC hardware component."
            if emit_fn:
                emit_fn('agent_complete', {
                    "query": clean_prompt,
                    "original_query": prompt.strip(),
                    "category": 'Not compatible (N/A)',
                    "bestOffer": None,
                    "allOffers": [],
                    "summary": state["summary"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            return state

        if is_url:
            offer = await self.extract_direct_page(clean_prompt, self.detect_retailer(clean_prompt), category)
            if offer:
                state["scrapedOffers"].append(offer)
        else:
            RETAILERS = [
                {'name': 'Micro Center', 'domain': 'microcenter.com'},
                {'name': 'Newegg', 'domain': 'newegg.com'},
                {'name': 'Amazon', 'domain': 'amazon.com'},
                {'name': 'Best Buy', 'domain': 'bestbuy.com'},
                {'name': 'B&H', 'domain': 'bhphotovideo.com'},
                {'name': 'eBay', 'domain': 'ebay.com'}
            ]

            # Launch concurrent scraping jobs using a semaphore to avoid hitting Tavily/Firecrawl concurrency limits too hard
            sem = asyncio.Semaphore(3)

            async def scrape_and_persist(r):
                async with sem:
                    try:
                        offer = await asyncio.wait_for(
                            self.scrape_retailer_accurate_offer(clean_prompt, r['name'], r['domain'], category),
                            timeout=32.0
                        )
                    except asyncio.TimeoutError:
                        print(f"⚠️ [Retailer Timeout] {r['name']} exceeded 32s — proceeding with other retailers")
                        offer = None
                    except Exception as e:
                        print(f"⚠️ [Retailer Error] {r['name']}: {e}")
                        offer = None

                    if offer and offer.get('price', 0) > 0:
                        state["scrapedOffers"].append(offer)
                        
                        try:
                            comp_id = f"comp-{re.sub(r'[^a-z0-9]+', '-', clean_prompt.lower())}-{offer['retailer'].lower().replace(' ', '-')}"
                            
                            # Preserve and append to PriceHistory
                            existing_specs = {}
                            offer_lowest_90d = offer['price']
                            try:
                                exist_check = await asyncio.to_thread(supabase.table('hardware_components').select('specs, lowest_price_90d').eq('id', comp_id).execute)
                                if exist_check.data and len(exist_check.data) > 0:
                                    raw_s = exist_check.data[0].get('specs')
                                    existing_specs = json.loads(raw_s) if isinstance(raw_s, str) else (raw_s or {})
                                    if exist_check.data[0].get('lowest_price_90d'):
                                        offer_lowest_90d = min(float(exist_check.data[0]['lowest_price_90d']), float(offer['price']))
                            except Exception:
                                pass

                            price_history = existing_specs.get('PriceHistory', [])
                            price_history.append({
                                "price": offer['price'],
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "inStock": offer['inStock']
                            })
                            if len(price_history) > 180:
                                price_history = price_history[-180:]

                            # Calculate deal score for offer
                            orig_p = offer.get('originalPrice') or offer['price']
                            if orig_p and orig_p > offer['price']:
                                discount_pct = (orig_p - offer['price']) / orig_p
                                offer_deal_score = min(99, int(60 + discount_pct * 100))
                            elif offer['price'] <= offer_lowest_90d:
                                offer_deal_score = 90
                            else:
                                offer_deal_score = 60

                            query1 = supabase.table('hardware_components').upsert({
                                "id": comp_id,
                                "name": offer['title'],
                                "category": category,
                                "brand": offer['title'].split()[0] if offer['title'] else "Hardware",
                                "model": clean_prompt,
                                "specs": json.dumps({
                                    "AgentSummary": "Live Autonomous Scraping Engine",
                                    "InStock": offer['inStock'],
                                    "IsRefurbished": offer.get('isRefurbished', False),
                                    "OriginalPrice": offer.get('originalPrice'),
                                    "ScrapedAt": datetime.now(timezone.utc).isoformat(),
                                    "PriceHistory": price_history
                                }),
                                "msrp": offer.get('originalPrice') or offer['price'],
                                "current_price": offer['price'],
                                "lowest_price_90d": offer_lowest_90d,
                                "retailer": offer['retailer'],
                                "product_url": offer['url'],
                                "image_url": offer.get('imageUrl') or "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=600&q=80",
                                "rating": offer.get('rating'),
                                "deal_score": offer_deal_score,
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            })
                            await asyncio.to_thread(query1.execute)
                            print(f"[DB Persist Success] Saved \"{offer['retailer']}\" offer: \"{offer['title']}\" (${offer['price']:.2f}) with PriceHistory ({len(price_history)} snapshots)")
                        except Exception as e:
                            print(f"[Agent Incremental Persistence Error]: {e}")
                            
                        if emit_fn:
                            emit_fn('retailer_found', {
                                "query": clean_prompt,
                                "original_query": prompt.strip(),
                                "retailer": offer['retailer'],
                                "price": offer['price'],
                                "title": offer['title'],
                                "url": offer['url'],
                                "inStock": offer['inStock'],
                                "isRefurbished": offer.get('isRefurbished', False),
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            })

            tasks = [scrape_and_persist(r) for r in RETAILERS]
            await asyncio.gather(*tasks)

        # Sort all scraped retailer offers: Available (inStock) items first, then lowest price
        def sort_offers(offer):
            return (0 if offer['inStock'] else 1, offer['price'])

        state["scrapedOffers"].sort(key=sort_offers)

        if len(state["scrapedOffers"]) > 0:
            state["bestOffer"] = state["scrapedOffers"][0]
            stock_status = 'In Stock' if state["bestOffer"]['inStock'] else 'Out of Stock / Backorder'
            state["summary"] = f"Evaluated {len(state['scrapedOffers'])} live retailer listings. Cheapest available offer: ${state['bestOffer']['price']:.2f} at {state['bestOffer']['retailer']} ({stock_status})."
        else:
            state["summary"] = f"No live prices found across retailers for \"{clean_prompt}\"."

        # Single consolidated user watchlist entry
        if user_id and pending_id and state.get("bestOffer"):
            try:
                best = state["bestOffer"]
                primary_comp_id = f"comp-{re.sub(r'[^a-z0-9]+', '-', clean_prompt.lower())}"
                
                # Check if this user already has an entry for this clean_prompt / component_id
                existing_check = supabase.table('watchlist_items').select('*').eq('component_id', primary_comp_id)
                if user_id:
                    existing_check = existing_check.eq('user_id', user_id)
                existing_res = await asyncio.to_thread(existing_check.execute)

                if existing_res.data and len(existing_res.data) > 0:
                    existing_row = existing_res.data[0]
                    target_id = existing_row['id']
                    prior_price = existing_row.get('current_price') or existing_row.get('all_time_low')
                    prior_atl = existing_row.get('all_time_low') or best['price']

                    wl_row = {
                        "component_name": best.get('title') or clean_prompt,
                        "category": category,
                        "target_price": round(best['price'] * 0.9, 2),
                        "previous_price_24h": prior_price if prior_price and prior_price != best['price'] else existing_row.get('previous_price_24h', best['price']),
                        "all_time_low": min(prior_atl, best['price']),
                    }
                    await asyncio.to_thread(
                        supabase.table('watchlist_items').update(wl_row).eq('id', target_id).execute
                    )
                    print(f"[Watchlist Persist Success] Updated single entry for '{clean_prompt}' in watchlist_items")
                else:
                    wl_row = {
                        "component_id": primary_comp_id,
                        "component_name": best.get('title') or clean_prompt,
                        "category": category,
                        "target_price": round(best['price'] * 0.9, 2),
                        "previous_price_24h": best['price'],
                        "previous_price_7d": best['price'],
                        "previous_price_30d": best['price'],
                        "all_time_low": best['price'],
                    }
                    if user_id:
                        wl_row["user_id"] = user_id
                    await asyncio.to_thread(
                        supabase.table('watchlist_items').insert(wl_row).execute
                    )
                    print(f"[Watchlist Persist Success] Created single baseline entry for '{clean_prompt}' in watchlist_items")
            except Exception as wl_e:
                print(f"[Watchlist Persist Notice]: {wl_e}")

        if state.get("bestOffer"):
            try:
                query = supabase.table('hardware_components').select('current_price').ilike('model', f"%{clean_prompt}%").order('updated_at', desc=True).limit(1)
                res = await asyncio.to_thread(query.execute)
                previous_price = res.data[0]['current_price'] if res.data else None

                if previous_price is not None:
                    diff = state["bestOffer"]['price'] - previous_price
                    if diff < -0.5:
                        state["priceChange"] = 'drop'
                        state["previousPrice"] = previous_price
                        if emit_fn:
                            emit_fn('price_drop', {
                                "query": clean_prompt,
                                "original_query": prompt.strip(),
                                "retailer": state["bestOffer"]['retailer'],
                                "previousPrice": previous_price,
                                "newPrice": state["bestOffer"]['price'],
                                "savings": f"{abs(diff):.2f}",
                                "url": state["bestOffer"]['url'],
                                "title": state["bestOffer"]['title'],
                                "category": category,
                                "timestamp": datetime.now(timezone.utc).isoformat()
                            })
                    elif diff > 0.5:
                        state["priceChange"] = 'increase'
                        state["previousPrice"] = previous_price
                    else:
                        state["priceChange"] = 'unchanged'
                else:
                    state["priceChange"] = 'new'

                component_id = f"agent-{re.sub(r'[^a-z0-9]+', '-', clean_prompt.lower())}-{re.sub(r'[^a-z0-9]+', '-', state['bestOffer']['retailer'].lower())}"
                
            except Exception as e:
                print(f"[Agent Persistence Error]: {e}")

        if emit_fn:
            emit_fn('agent_complete', {
                "query": clean_prompt,
                "original_query": prompt.strip(),
                            "timestamp": datetime.now(timezone.utc).isoformat()
            })

        return state

    async def extract_direct_page(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        offer = await self.direct_http_extract(url, retailer_name, category, model_query)
        if not offer:
            offer = await self.firecrawl_extract(url, retailer_name, category, model_query)
        return offer

    def is_semantic_product_match(self, title: str, query: str, category: str = None) -> bool:
        """
        Adaptable semantic match: verifies title matches requested product without arbitrary price bounds.
        """
        if not title or not query:
            return False

        lower_t = title.lower()
        lower_q = query.lower()
        clean_t = re.sub(r'[^a-z0-9]', '', lower_t)

        # 1. Reject junk, broken items, and parts-only listings
        bad_keywords = [
            'for parts', 'broken', 'box only', 'read description', 'empty box', 
            'sticker only', 'packaging only', 'manual only', 'dummy', 'poster', 'as is', 'case badge'
        ]
        if any(b in lower_t and b not in lower_q for b in bad_keywords):
            return False

        # 2. Reject accessory and part replacement listings unless query explicitly requested it
        ACCESSORY_KEYWORDS = [
            'cable', '12vhpwr', 'adapter', 'bracket', 'backplate', 'anti-sag', 'holder', 'support bracket',
            'water block', 'waterblock', 'block only', 'heatsink only', 'cooler only', 'shroud', 'replacement fan',
            'gpu fan', 'case badge', 'sticker', 'poster', 'skin', 'wrap', 'keycap', 'mining rig', 'dummy card',
            'chassis frame', 'riser cable', 'extension cable', 'sleeved cable', 'mounting kit', 'liquid cooler block',
            'thermal pad', 'copper shim', 'stand only', 'empty box', 'decal'
        ]
        if not any(k in lower_q for k in ['cable', 'adapter', 'bracket', 'backplate', 'block', 'shroud', 'fan', 'pad', 'shim', 'mount', 'accessory']):
            if any(re.search(r'\b' + re.escape(ak) + r'\b', lower_t) for ak in ACCESSORY_KEYWORDS):
                return False

        # 3. Reject multi-model keyword stuffing spam (e.g. "RTX 5070 Ti 5080 5090" in title)
        found_gpus = re.findall(r'\b(5090|5080|5070\s*ti|5070|5060\s*ti|5060|4090|4080|4070\s*ti|4070|4060\s*ti|4060|3090|3080|3070|3060)\b', lower_t)
        if len(set(re.sub(r'\s+', '', g) for g in found_gpus)) > 1:
            return False

        # 4. Reject prebuilt PCs / Laptops when looking for individual components
        if category in ['GPU', 'CPU', 'Motherboard', 'RAM', 'Storage', 'Power Supply', 'Case', 'Cooling'] or not category:
            if not any(k in lower_q for k in ['pc', 'desktop', 'prebuilt', 'laptop', 'system', 'notebook']):
                if any(p in lower_t for p in ['desktop pc', 'gaming pc', 'gaming desktop', 'laptop', 'prebuilt pc', 'complete pc', 'all-in-one', 'notebook']):
                    return False

        # 5. Extract core numeric/model identifiers from query and verify presence
        q_raw = re.sub(r'([a-zA-Z]{2,})(\d+)', r'\1 \2', query)
        q_raw = re.sub(r'(\d+)([a-zA-Z]{2,})', r'\1 \2', q_raw)
        q_tokens = [w for w in re.sub(r'[^a-z0-9\s]', ' ', q_raw.lower()).split() if len(w) > 0]
        
        digit_tokens = [w for w in q_tokens if re.search(r'\d', w)]
        pure_digits = [re.findall(r'\d+', w)[0] for w in digit_tokens if re.findall(r'\d+', w)]
        if pure_digits and not all(d in clean_t for d in pure_digits):
            return False

        # 6. If query specifies critical modifiers (e.g. 'ti', 'super', 'wifi', 'white', 'ddr5', '2tb'), ensure title matches
        CRITICAL_MODIFIERS = {'plus', 'super', 'ti', 'xt', 'xtx', 'wifi', 'white', 'liquid', 'wireless', 'ddr4', 'ddr5', '1tb', '2tb', '4tb', '32gb', '64gb'}
        query_modifiers = [w for w in q_tokens if w in CRITICAL_MODIFIERS]
        if query_modifiers and not all(m in clean_t for m in query_modifiers):
            return False

        return True

    async def filter_matching_titles(self, titles: list[str], query: str, category: str) -> list[bool]:
        return [self.is_semantic_product_match(t, query, category) for t in titles]

    def extract_json_ld(self, soup: BeautifulSoup, model_query: str = None) -> dict | None:
        """
        Parses Schema.org JSON-LD standard embedded on Best Buy, B&H, Newegg, Micro Center, eBay, Amazon, etc.
        """
        for script in soup.find_all('script', type='application/ld+json'):
            if not script.string:
                continue
            try:
                raw_data = json.loads(script.string)
                items = []
                if isinstance(raw_data, list):
                    items = raw_data
                elif isinstance(raw_data, dict):
                    if '@graph' in raw_data and isinstance(raw_data['@graph'], list):
                        items = raw_data['@graph']
                    else:
                        items = [raw_data]

                for item in items:
                    item_type = str(item.get('@type', '')).lower()
                    if 'product' in item_type or 'itempage' in item_type or 'individualproduct' in item_type:
                        name = item.get('name') or item.get('headline')
                        offers = item.get('offers')
                        if not offers:
                            continue

                        offer_list = offers if isinstance(offers, list) else [offers]
                        for off in offer_list:
                            if not isinstance(off, dict):
                                continue
                            raw_price = off.get('price') or off.get('lowPrice')
                            if raw_price is not None:
                                try:
                                    price_val = float(str(raw_price).replace('$', '').replace(',', '').strip())
                                except (ValueError, TypeError):
                                    continue

                                if price_val > 0:
                                    avail_str = str(off.get('availability', '')).lower()
                                    is_in_stock = True
                                    if any(s in avail_str for s in ['outofstock', 'discontinued', 'soldout', 'backorder']):
                                        is_in_stock = False

                                    cond_str = str(off.get('itemCondition', '')).lower()
                                    is_refurbished = any(c in cond_str for c in ['refurbished', 'used', 'damaged'])

                                    image_url = item.get('image')
                                    if isinstance(image_url, list) and image_url:
                                        image_url = image_url[0]
                                    elif isinstance(image_url, dict):
                                        image_url = image_url.get('url')

                                    brand = item.get('brand')
                                    if isinstance(brand, dict):
                                        brand = brand.get('name')

                                    return {
                                        "title": name,
                                        "price": price_val,
                                        "inStock": is_in_stock,
                                        "isRefurbished": is_refurbished,
                                        "imageUrl": image_url if isinstance(image_url, str) else None,
                                        "brand": str(brand) if brand else None,
                                        "source": "json-ld"
                                    }
            except Exception:
                continue
        return None

    def extract_opengraph_microdata(self, soup: BeautifulSoup) -> dict | None:
        """
        Parses OpenGraph and Microdata meta tags.
        """
        price = None
        for tag in soup.select('meta[property="og:price:amount"], meta[property="product:price:amount"], meta[itemprop="price"], meta[name="twitter:data1"]'):
            content = tag.get('content') or tag.get('value')
            if content:
                m = re.search(r'([0-9,]+(?:\.[0-9]{2})?)', content)
                if m:
                    try:
                        val = float(m.group(1).replace(',', ''))
                        if val > 0:
                            price = val
                            break
                    except ValueError:
                        pass

        if not price:
            return None

        title_tag = soup.select_one('meta[property="og:title"], meta[name="twitter:title"]')
        title = title_tag.get('content').strip() if title_tag and title_tag.get('content') else None

        img_tag = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
        image_url = img_tag.get('content').strip() if img_tag and img_tag.get('content') else None

        avail_tag = soup.select_one('meta[property="og:availability"], meta[itemprop="availability"]')
        is_in_stock = True
        if avail_tag and avail_tag.get('content'):
            avail_val = avail_tag['content'].lower()
            if any(s in avail_val for s in ['outofstock', 'discontinued', 'soldout']):
                is_in_stock = False

        return {
            "title": title,
            "price": price,
            "inStock": is_in_stock,
            "isRefurbished": False,
            "imageUrl": image_url,
            "brand": None,
            "source": "opengraph"
        }

    def extract_retailer_buybox_dom(self, soup: BeautifulSoup, retailer_name: str) -> dict | None:
        """
        Clean, targeted DOM buybox parser for each supported retailer.
        """
        price = None
        title = None
        in_stock = True
        is_refurbished = False

        if retailer_name == 'Amazon':
            if 'robot check' in soup.text.lower() or 'enter the characters you see below' in soup.text.lower():
                return None
            avail = soup.select_one('#availability')
            if avail and ('currently unavailable' in avail.text.lower() or 'out of stock' in avail.text.lower()):
                return {"out_of_stock": True}

            price_elem = soup.select_one('#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #corePrice_feature_div .a-price .a-offscreen, #corePrice_desktop .a-price .a-offscreen, #apex_desktop .a-price .a-offscreen')
            if price_elem:
                m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                if m:
                    try: price = float(m.group(1).replace(',', ''))
                    except ValueError: pass

            t_elem = soup.select_one('#productTitle')
            if t_elem: title = t_elem.text.strip()

        elif retailer_name == 'Newegg':
            for sp in soup.select('[class*="sponsored"], .item-sponsored, .recommended-box, .swiper, .carousel, .featured-seller'):
                sp.decompose()
            main_pane = soup.select_one('.product-pane, .product-buy-box, .product-main, #product-details') or soup
            price_elem = main_pane.select_one('.price-current')
            if price_elem:
                price_strong = price_elem.select_one('strong')
                price_sup = price_elem.select_one('sup')
                if price_strong:
                    price_str = price_strong.text.replace('$', '').replace(',', '').strip()
                    frac = price_sup.text.strip() if price_sup else ".00"
                    try: price = float(f"{price_str}{frac}")
                    except ValueError: pass
                else:
                    m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                    if m:
                        try: price = float(m.group(1).replace(',', ''))
                        except ValueError: pass
            t_elem = main_pane.select_one('.product-title, h1.product-title, h1')
            if t_elem: title = t_elem.text.strip()

            inv = soup.select_one('.product-inventory')
            if inv and 'out of stock' in inv.text.lower():
                return {"out_of_stock": True}

        elif retailer_name == 'Best Buy':
            price_elem = soup.select_one('.priceView-customer-price span[aria-hidden="true"], .priceView-hero-price span[aria-hidden="true"], div[data-testid="customer-price"] span[aria-hidden="true"], .pricing-price span')
            if price_elem:
                m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                if m:
                    try: price = float(m.group(1).replace(',', ''))
                    except ValueError: pass
            t_elem = soup.select_one('.sku-title h1, h1[class*="product-title"], h1')
            if t_elem: title = t_elem.text.strip()

            btn = soup.select_one('.add-to-cart-button, button[data-button-state]')
            if btn and ('sold_out' in btn.get('data-button-state', '').lower() or 'sold out' in btn.text.lower()):
                return {"out_of_stock": True}

        elif retailer_name == 'B&H':
            price_elem = soup.select_one('[data-selenium="pricingPrice"], .price__9gLfjPSjp')
            if price_elem:
                m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                if m:
                    try: price = float(m.group(1).replace(',', ''))
                    except ValueError: pass
            t_elem = soup.select_one('[data-selenium="productTitle"], h1[data-selenium="productTitle"], h1')
            if t_elem: title = t_elem.text.strip()

            avail = soup.select_one('.shippingAvail_yL7x0I4P, [data-selenium="stockStatus"], [data-selenium="availability"]')
            avail_text = avail.text.lower() if avail else ''
            if 'no longer available' in avail_text or 'discontinued' in avail_text:
                return {"out_of_stock": True}

        elif retailer_name == 'eBay':
            for plan in soup.select('.x-additional-services, [data-testid*="additional-services"], [class*="protection-plan"], [class*="warranty"], [data-testid*="warranty"], .insurance-plan'):
                plan.decompose()
            price_elem = soup.select_one('.x-price-primary, [data-testid="x-price-primary"], .x-bin-price .x-price-primary')
            if price_elem:
                spans = price_elem.select('.ux-textspans')
                valid_spans = [s for s in spans if 'strikethrough' not in ''.join(s.get('class', [])).lower()]
                target_text = valid_spans[0].text if valid_spans else price_elem.text
                m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', target_text)
                if m:
                    try: price = float(m.group(1).replace(',', ''))
                    except ValueError: pass
            t_elem = soup.select_one('.x-item-title__mainTitle span.ux-textspans, .x-item-title__mainTitle')
            if t_elem: title = t_elem.text.strip()

            cond_elem = soup.select_one('.x-item-condition-text')
            if cond_elem:
                cond_text = cond_elem.text.lower()
                if 'used' in cond_text or 'refurbished' in cond_text:
                    is_refurbished = True
                if 'for parts' in cond_text or 'not working' in cond_text:
                    return None

        elif retailer_name == 'Micro Center':
            price_elem = soup.select_one('#pricing, #pricing2')
            if price_elem:
                content_val = price_elem.get('content')
                if content_val:
                    m = re.search(r'([0-9,]+(?:\.[0-9]{2})?)', content_val)
                    if m:
                        try: price = float(m.group(1).replace(',', ''))
                        except ValueError: pass
                if not price:
                    m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                    if m:
                        try: price = float(m.group(1).replace(',', ''))
                        except ValueError: pass
            t_elem = soup.select_one('[data-name], .product-header h1')
            if t_elem and t_elem.get('data-name'):
                title = t_elem['data-name'].strip()
            elif t_elem:
                title = t_elem.text.strip()

            inv = soup.select_one('.inventoryCnt, .out-of-stock, .unavailable')
            if inv and ('sold out' in inv.text.lower() or 'no longer carried' in inv.text.lower()):
                return {"out_of_stock": True}

        if price and price > 0:
            return {
                "title": title,
                "price": price,
                "inStock": in_stock,
                "isRefurbished": is_refurbished,
                "imageUrl": None,
                "brand": None,
                "source": "buybox-dom"
            }
        return None

    def is_page_out_of_stock(self, soup: BeautifulSoup, retailer_name: str) -> bool:
        """
        Verifies if the page or buybox explicitly indicates out of stock / discontinued / no longer available.
        """
        lower_page = (soup.text or '')[:4000].lower()

        # 1. Retailer-specific critical indicators
        if retailer_name == 'B&H':
            avail = soup.select_one('[data-selenium="stockStatus"], [data-selenium="availability"], .shippingAvail_yL7x0I4P, [class*="stockStatus"], [class*="availability"]')
            if avail:
                text = avail.text.lower()
                if any(s in text for s in ['no longer available', 'discontinued', 'out of stock', 'sold out', 'backorder', 'not available']):
                    return True
            if 'no longer available' in lower_page or 'discontinued' in lower_page:
                return True

        elif retailer_name == 'Best Buy':
            btn = soup.select_one('.add-to-cart-button, button[data-button-state]')
            if btn:
                btn_state = btn.get('data-button-state', '').lower()
                btn_text = btn.text.lower()
                if 'sold_out' in btn_state or 'sold out' in btn_text or 'unavailable' in btn_text:
                    return True
            fulfillment = soup.select_one('.fulfillment-add-to-cart-button, .fulfillment-fulfillment-summary')
            if fulfillment and ('sold out' in fulfillment.text.lower() or 'currently unavailable' in fulfillment.text.lower()):
                return True
            if 'sold out' in lower_page[:1500] or 'currently unavailable' in lower_page[:1500]:
                return True

        elif retailer_name == 'Newegg':
            inv = soup.select_one('.product-inventory, .product-buy-box')
            if inv and any(s in inv.text.lower() for s in ['out of stock', 'sold out', 'discontinued', 'auto notify']):
                return True

        elif retailer_name == 'Micro Center':
            inv = soup.select_one('.inventoryCnt, .out-of-stock, .unavailable, .availabilityTrunc')
            if inv and any(s in inv.text.lower() for s in ['sold out', 'no longer carried', 'not available', '0 in stock', 'out of stock']):
                return True

        elif retailer_name == 'Amazon':
            avail = soup.select_one('#availability')
            if avail and any(s in avail.text.lower() for s in ['currently unavailable', 'out of stock', 'temporarily out of stock']):
                return True

        elif retailer_name == 'eBay':
            ended = soup.select_one('.x-ended-item-msg, .msg-error, .ux-notice')
            if ended and any(s in ended.text.lower() for s in ['ended', 'out of stock', 'this listing has ended', 'this listing was ended']):
                return True

        # 2. Universal stock badge search
        raw_badges = soup.select('.stock-status, .out-of-stock, .badge-out-of-stock, [data-stock="out_of_stock"]')
        for b in raw_badges:
            if any(s in b.text.lower() for s in ['out of stock', 'sold out', 'no longer available', 'discontinued']):
                return True

        return False

    async def parse_page_content(self, html_content: str, markdown_content: str, url: str, retailer_name: str, category: str, model_query: str = None) -> dict | None:
        if not html_content:
            return None

        soup = BeautifulSoup(html_content, 'lxml')

        # Tier 1: Schema.org JSON-LD
        offer = self.extract_json_ld(soup, model_query)

        # Tier 2: OpenGraph & Microdata
        if not offer or offer.get('price', 0) == 0:
            og_offer = self.extract_opengraph_microdata(soup)
            if og_offer and og_offer.get('price', 0) > 0:
                offer = og_offer

        # Tier 3: Dedicated Retailer Buybox DOM
        if not offer or offer.get('price', 0) == 0:
            dom_offer = self.extract_retailer_buybox_dom(soup, retailer_name)
            if dom_offer:
                if dom_offer.get('out_of_stock'):
                    return {"out_of_stock": True}
                offer = dom_offer

        # Tier 4: AI Extraction on clean markdown if structured data missing
        if not offer or offer.get('price', 0) == 0:
            groq_data = await self.parse_with_groq(markdown_content or html_content[:8000], model_query, retailer_name, category)
            if groq_data and groq_data.get('price', 0) > 0:
                offer = {
                    "title": groq_data.get('title'),
                    "price": groq_data['price'],
                    "originalPrice": groq_data.get('originalPrice'),
                    "inStock": groq_data.get('inStock', True),
                    "isRefurbished": groq_data.get('isRefurbished', False),
                    "brand": groq_data.get('brand'),
                    "source": "groq-ai"
                }

        # Multi-layer stock validation: check if page indicates out of stock / discontinued
        page_oos = self.is_page_out_of_stock(soup, retailer_name)
        if page_oos:
            if offer:
                offer['inStock'] = False
            else:
                return {"out_of_stock": True}

        if not offer or offer.get('price', 0) <= 0:
            return {"out_of_stock": True} if page_oos else None

        title = offer.get('title') or model_query or ''
        full_title_for_check = f"{title} {url}"

        # Semantic Match Check
        if model_query:
            if not self.is_semantic_product_match(full_title_for_check, model_query, category):
                print(f"⚠️ [Semantic Mismatch] {retailer_name}: '{title}' does not match query '{model_query}'")
                return None

        is_stock_final = offer.get('inStock', True) and not page_oos
        print(f"✅ [{offer.get('source', 'scraped').upper()} HIT] {retailer_name}: Found price ${offer['price']:.2f} (InStock: {is_stock_final}) -> {title[:60]}")
        return {
            "retailer": retailer_name,
            "title": title,
            "price": offer['price'],
            "originalPrice": offer.get('originalPrice'),
            "inStock": is_stock_final,
            "isRefurbished": offer.get('isRefurbished', False),
            "url": url,
            "imageUrl": offer.get('imageUrl'),
            "brand": offer.get('brand'),
            "source": offer.get('source')
        }

    async def scrape_retailer_accurate_offer(self, model_query: str, retailer_name: str, domain_pattern: str, category: str) -> dict:
        print(f"[Tavily Search] Querying {retailer_name} for \"{model_query}\" ({category})...")
        
        for attempt in range(len(TAVILY_API_KEYS)):
            try:
                # 1. Search with Tavily to get candidate URLs
                async with httpx.AsyncClient() as client:
                    res = await client.post('https://api.tavily.com/search', json={
                        "api_key": self.get_tavily_key(),
                        "query": f"buy {model_query} price",
                        "search_depth": "advanced",
                        "include_domains": [domain_pattern],
                        "include_raw_content": False,
                        "max_results": 10
                    }, timeout=15.0)
                
                if res.status_code != 200:
                    raise Exception(f"Tavily search failed: {res.text}")
                    
                data = res.json()
                results = data.get('results', [])
                
                # Batch filter candidate titles
                titles_to_check = [r.get('title', '') for r in results]
                match_results = await self.filter_matching_titles(titles_to_check, model_query, category)

                valid_hits = []
                for i, hit in enumerate(results):
                    if i < len(match_results) and not match_results[i]:
                        print(f"⚠️ [Tavily Title Mismatch] Skipping: {hit.get('title')}")
                        continue

                    full_url = hit.get('url', '')
                    if not self.is_valid_direct_product_url(full_url, domain_pattern):
                        continue
                        
                    clean_url = full_url.replace('/reviews/', '').split('?')[0]
                    valid_hits.append({
                        'url': clean_url,
                        'title': hit.get('title', '')
                    })

                if not valid_hits:
                    return None
                    
                # Try direct extraction on candidate URLs
                for candidate in valid_hits[:5]:
                    # 1. Try Direct HTTP fetch first (fast, browser headers, 0 credit cost)
                    offer = await self.direct_http_extract(candidate['url'], retailer_name, category, model_query)
                    
                    # 2. If Direct HTTP failed or was blocked, try Firecrawl proxy
                    if not offer or (offer.get('price', 0) == 0 and not offer.get('out_of_stock')):
                        offer = await self.firecrawl_extract(candidate['url'], retailer_name, category, model_query)

                    if offer:
                        if offer.get('out_of_stock') or not offer.get('inStock', True):
                            print(f"⚠️ [Confirmed Out of Stock] {retailer_name}: '{offer.get('title', candidate['title'])}' is out of stock / discontinued. Skipping to next candidate...")
                            continue
                        if offer.get('price', 0) > 0:
                            print(f"✅ [CHEAPEST AVAILABLE {retailer_name.upper()} OFFER] \"${offer['price']}\" -> {offer['title'][:60]}")
                            return offer

                # Zero snippet guessing: If direct page scraping could not confirm an in-stock offer, return None
                return None
            except Exception as e:
                print(f"[Tavily Search Error] {retailer_name}: {e}")
                self.rotate_tavily_key()
        return None

    async def direct_http_extract(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        """Direct HTTP scraper with realistic browser headers when Firecrawl is blocked/unavailable."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.google.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        }
        try:
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    return await self.parse_page_content(res.text, "", url, retailer_name, category, model_query)
        except Exception:
            pass
        return None

    async def firecrawl_extract(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        app = self.get_firecrawl_app()
        if not app:
            return None
            
        print(f"[Firecrawl] Extracting {url} ...")
        scrape_timeout = 10.0
        try:
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(lambda: app.scrape_url(url, formats=['html', 'markdown'])),
                    timeout=scrape_timeout
                )
            except asyncio.TimeoutError:
                print(f"⚠️ [Firecrawl Timeout] {retailer_name} took >{scrape_timeout}s — skipping URL")
                return None
            except Exception as e:
                error_str = str(e).lower()
                if "payment" in error_str or "credits" in error_str or "401" in error_str or "402" in error_str or "rate limit" in error_str or "429" in error_str:
                    print(f"[Firecrawl] Rate/credit limit — skipping {url}")
                    return None
                else:
                    print(f"[Firecrawl Error] {retailer_name}: {e}")
                    return None
            
            if not res:
                return None
                
            html_content = ""
            markdown_content = ""
            
            if hasattr(res, 'get'):
                html_content = res.get('html', '')
                markdown_content = res.get('markdown', '')
            else:
                markdown_content = getattr(res, 'markdown', '') or getattr(res, 'page_content', '') or getattr(res, 'text', '')
                html_content = getattr(res, 'html', '')
                if not html_content and hasattr(res, 'metadata') and isinstance(res.metadata, dict):
                    html_content = res.metadata.get('html', '')
                    if not markdown_content:
                        markdown_content = res.metadata.get('markdown', '')

            if not html_content:
                html_content = markdown_content
                
            return await self.parse_page_content(html_content, markdown_content, url, retailer_name, category, model_query)
        except Exception as e:
            print(f"⚠️ [Firecrawl Extraction Error] {url}: {e}")
            return None

    async def parse_with_groq(self, markdown_content: str, query: str, retailer: str, category: str) -> dict:
        if not GROQ_API_KEY:
            return {}

        clean_markdown = re.sub(r'##\s*People who viewed this item also viewed[\s\S]*?(?=##|\Z)', '', markdown_content, flags=re.I)
        clean_markdown = re.sub(r'##\s*Similar items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)
        clean_markdown = re.sub(r'##\s*Sponsored items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)
        clean_markdown = re.sub(r'##\s*Compare with similar items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)
        clean_markdown = re.sub(r'(?:additional service available|protection plan|allstate|squaretrade|asurion|applecare|extended warranty)[\s\S]*?(?=\n\n|\Z)', '', clean_markdown, flags=re.I)

        system_prompt = (
            f"You are an expert product data extraction agent parsing a {retailer} product page. "
            f"Extract product details for the target item: '{query}' ({category}). "
            "CRITICAL RULES:\n"
            "1. Extract the current active BUYBOX selling price for the MAIN product.\n"
            "2. NEVER extract protection plans (e.g. '$28.00'), warranties, financing (e.g. '$55/mo'), or shipping fees.\n"
            "3. NEVER extract prices of related or recommended items from carousels.\n"
            "4. Return ONLY valid JSON with keys: price (float), originalPrice (float or null), title (str), brand (str or null), inStock (bool)."
        )

        models_to_try = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"]
        for model_name in models_to_try:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json={
                            "model": model_name,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": f"Extract info from this markdown:\n\n{clean_markdown[:7000]}"}
                            ],
                            "response_format": {"type": "json_object"},
                            "temperature": 0
                        },
                        timeout=10.0
                    )
                    if res.status_code == 200:
                        return json.loads(res.json()["choices"][0]["message"]["content"])
                    elif res.status_code == 429:
                        print(f"[Groq 429] Rate limit on {model_name} — trying next model fallback...")
                        continue
            except Exception as e:
                print(f"[Groq Price Parse Error with {model_name}] {e}")

        return {}

    def is_valid_direct_product_url(self, url: str, domain_pattern: str) -> bool:
        lower = url.lower()
        if any(x in lower for x in ['/reviews/', 'reviews', 'questions', '/forum/', '/blog/', 'searchpage.jsp', '/s?k=', '/p/pl', '/openbox']):
            return False
            
        if 'microcenter.com' in domain_pattern: return '/product/' in lower
        if 'amazon.com' in domain_pattern: return '/dp/' in lower or '/gp/product/' in lower
        if 'newegg.com' in domain_pattern: return '/p/' in lower and not '/p/pl' in lower
        if 'bestbuy.com' in domain_pattern: return ('/site/' in lower and '.p' in lower) or '/product/' in lower
        if 'bhphotovideo.com' in domain_pattern: return '/c/product/' in lower and not '/accessories' in lower
        if 'ebay.com' in domain_pattern: return '/itm/' in lower
        return True

    def detect_retailer(self, text: str) -> str:
        lower = text.lower()
        if 'microcenter' in lower: return 'Micro Center'
        if 'newegg' in lower: return 'Newegg'
        if 'bestbuy' in lower: return 'Best Buy'
        if 'bhphotovideo' in lower: return 'B&H'
        if 'ebay' in lower: return 'eBay'
        return 'Amazon'

    async def detect_category(self, text: str) -> str:
        if not GROQ_API_KEY:
            return 'Not compatible (N/A)'
            
        system_prompt = (
            "Classify the following query or URL into ONE of these strict categories: "
            "GPU, CPU, RAM, Motherboard, Storage, Power Supply, Case, Cooling, Monitor, Peripherals, Networking. "
            "If it is NOT a computer component or peripheral, return EXACTLY: Not compatible (N/A). "
            "Return ONLY the category name. Do not include any other text."
        )
        models_to_try = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
        for model_name in models_to_try:
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json={
                            "model": model_name,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": f"Query: {text}"}
                            ],
                            "temperature": 0
                        },
                        timeout=5.0
                    )
                if res.status_code == 200:
                    category = res.json()["choices"][0]["message"]["content"].strip()
                    valid_categories = ['GPU', 'CPU', 'RAM', 'Motherboard', 'Storage', 'Power Supply', 'Case', 'Cooling', 'Monitor', 'Peripherals', 'Networking']
                    if category in valid_categories:
                        return category
                    for vc in valid_categories:
                        if vc.lower() in category.lower():
                            return vc
            except Exception as e:
                print(f"[Groq Category Detect Error with {model_name}] {e}")
                
        return 'Not compatible (N/A)'

    def normalize_model(self, text: str, fallback: str) -> str:
        if fallback and len(fallback.strip()) > 2:
            return fallback.strip()
        clean = re.sub(r'^(asus|msi|gigabyte|zotac|evga|sapphire|xfx|pny|powercolor|asrock|intel|amd|nvidia|corsair|g\.skill|samsung|crucial|western digital|wd)\s+', '', text, flags=re.I)
        clean = re.sub(r'\s+(graphics card|video card|processor|desktop processor|cpu|gpu|ddr4|ddr5|ram|nvme|ssd|motherboard|power supply|edition|oc|gaming).*$', '', clean, flags=re.I).strip()
        return clean or text
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
                            offer_model_group = self.normalize_model(offer['title'], clean_prompt)
                            offer_component_id = f"comp-{re.sub(r'[^a-z0-9]+', '-', offer_model_group.lower())}-{re.sub(r'[^a-z0-9]+', '-', offer['retailer'].lower())}"

                            offer_msrp = offer.get('originalPrice') if offer.get('originalPrice') and offer.get('originalPrice') > offer['price'] else offer['price']
                            offer_deal_score = min(100, max(50, round(50 + ((offer_msrp - offer['price']) / offer_msrp) * 100))) if offer_msrp > offer['price'] else 50

                            query1 = supabase.table('hardware_components').upsert({
                                "id": offer_component_id,
                                "name": offer['title'],
                                "category": category,
                                "brand": offer.get('brand') or offer['title'].split(' ')[0] or 'Hardware',
                                "model": offer_model_group,
                                "specs": json.dumps({
                                    "AgentSummary": "Live Scraping...",
                                    "InStock": offer['inStock'],
                                    "IsRefurbished": offer.get('isRefurbished', False),
                                    "OriginalPrice": offer.get('originalPrice'),
                                    "ScrapedAt": datetime.now(timezone.utc).isoformat()
                                }),
                                "msrp": offer_msrp,
                                "current_price": offer['price'],
                                "lowest_price_90d": offer['price'],
                                "retailer": offer['retailer'],
                                "product_url": offer['url'],
                                "image_url": offer.get('imageUrl') or "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=600&q=80",
                                "rating": offer.get('rating'),
                                "deal_score": offer_deal_score,
                                "updated_at": datetime.now(timezone.utc).isoformat()
                            })
                            await asyncio.to_thread(query1.execute)
                            print(f"[DB Persist Success] Saved \"{offer['retailer']}\" offer: \"{offer['title']}\" (${offer['price']:.2f}) to hardware_components")
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

                # Check if this user already has an entry for this clean_prompt / component_id
                existing_check = supabase.table('watchlist_items').select('id').eq('component_id', primary_comp_id)
                if user_id:
                    existing_check = existing_check.eq('user_id', user_id)
                existing_res = await asyncio.to_thread(existing_check.execute)

                if existing_res.data and len(existing_res.data) > 0:
                    target_id = existing_res.data[0]['id']
                    await asyncio.to_thread(
                        supabase.table('watchlist_items').update(wl_row).eq('id', target_id).execute
                    )
                    print(f"[Watchlist Persist Success] Updated single entry for '{clean_prompt}' in watchlist_items")
                else:
                    await asyncio.to_thread(
                        supabase.table('watchlist_items').insert(wl_row).execute
                    )
                    print(f"[Watchlist Persist Success] Created single entry for '{clean_prompt}' in watchlist_items")
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
                "bestOffer": state.get("bestOffer"),
                "allOffers": state["scrapedOffers"],
                "priceChange": state.get("priceChange"),
                "previousPrice": state.get("previousPrice"),
                "summary": state["summary"],
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

        return state

    async def extract_direct_page(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        return await self.firecrawl_extract(url, retailer_name, category, model_query)

    def extract_clean_snippet_price(self, text: str, model_query: str, category: str) -> float:
        if not text:
            return float("inf")

        # Split into distinct sentences / clauses without breaking decimal numbers
        clauses = re.split(r'(?:\.(?!\d)|[\n;\|•·–—])', text)
        valid_prices = []

        NOISE_KEYWORDS = [
            'plan', 'warranty', 'protection', 'geek squad', 'applecare', 'care pack',
            '/mo', '/month', 'per month', 'monthly', 'a month', 'financing', 'payments',
            'save', 'saving', 'discount', 'was', 'list price', 'retail price',
            'shipping', 'delivery', 'handling', 'postage', 'fee', 'trade-in', 'trade in'
        ]

        for clause in clauses:
            lower_clause = clause.lower().strip()
            # If the clause mentions warranty/financing/shipping/discounts, skip all prices inside it
            if any(re.search(r'\b' + re.escape(w) + r'\b', lower_clause) for w in NOISE_KEYWORDS):
                continue

            matches = re.findall(r'\$([0-9,]+(?:\.[0-9]{2})?)', clause)
            for m in matches:
                try:
                    val = float(m.replace(',', ''))
                    if val > 0 and self.is_price_sanity_valid(val, model_query, category):
                        valid_prices.append(val)
                except:
                    pass

        if not valid_prices:
            return float("inf")

        # Return the primary product price from the clean, non-noise clause
        return valid_prices[0]

    async def scrape_retailer_accurate_offer(self, model_query: str, retailer_name: str, domain_pattern: str, category: str) -> dict:
        print(f"[Tavily Search] Querying {retailer_name} for \"{model_query}\" ({category})...")
        
        for attempt in range(len(TAVILY_API_KEYS)):
            try:
                # 1. Search with Tavily to get URLs
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
                
                # Batch filter all titles to save Groq API calls
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
                    content = hit.get('content', '')
                    est_price = self.extract_clean_snippet_price(content, model_query, category)
                            
                    valid_hits.append({
                        'url': clean_url,
                        'title': hit.get('title', ''),
                        'est_price': est_price
                    })

                if not valid_hits:
                    return None
                    
                # Sort by estimated price (putting float('inf') at the end)
                valid_hits.sort(key=lambda x: x['est_price'])
                
                # Try Firecrawl on top 2 candidates
                for candidate in valid_hits[:2]:
                    offer = await self.firecrawl_extract(candidate['url'], retailer_name, category, model_query)
                    if offer:
                        print(f"✅ [CHEAPEST AVAILABLE {retailer_name.upper()} OFFER] \"${offer['price']}\" -> {offer['title'][:60]}")
                        return offer

                # Instant Fallback: use verified, noise-filtered Tavily snippet price
                for candidate in valid_hits:
                    if candidate['est_price'] < float('inf') and self.is_price_sanity_valid(candidate['est_price'], model_query, category):
                        print(f"✅ [TAVILY SNIPPET FALLBACK] {retailer_name}: Found price ${candidate['est_price']:.2f} -> {candidate['title'][:50]}")
                        return {
                            'retailer': retailer_name,
                            'title': candidate['title'] or f"{retailer_name} - {model_query}",
                            'price': candidate['est_price'],
                            'originalPrice': candidate['est_price'],
                            'url': candidate['url'],
                            'inStock': True,
                            'imageUrl': "https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80",
                            'isRefurbished': False
                        }
                return None
            except Exception as e:
                print(f"[Tavily Search Error] {retailer_name}: {e}")
                self.rotate_tavily_key()
        return None

    async def firecrawl_extract(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        app = self.get_firecrawl_app()
        if not app:
            print("[Firecrawl] API key missing")
            return None
            
        print(f"[Firecrawl] Extracting {url} ...")
        # Generous 22s timeout: allows JavaScript heavy pages (Best Buy, Micro Center, eBay) to fully render
        scrape_timeout = 22.0
        try:
            try:
                res = await asyncio.wait_for(
                    asyncio.to_thread(lambda: app.scrape_url(url, formats=['html', 'markdown'])),
                    timeout=scrape_timeout
                )
            except asyncio.TimeoutError:
                print(f"⚠️ [Firecrawl Timeout] {retailer_name} took >{scrape_timeout}s — using fallback")
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

            # Fallback if html wasn't specifically found
            if not html_content:
                html_content = markdown_content
                
            try:
                with open(f"scratch/dump_{retailer_name.replace(' ', '_')}.html", "w") as f:
                    f.write(html_content)
                with open(f"scratch/dump_{retailer_name.replace(' ', '_')}.md", "w") as f:
                    f.write(markdown_content)
            except: pass
                
            if not html_content:
                return None
            
            soup = BeautifulSoup(html_content, 'lxml')
            price = None
            title = None
            image_url = None
            in_stock = True
            is_refurbished = False
            
            try:
                og_image = soup.find('meta', property='og:image')
                if og_image and og_image.get('content'):
                    image_url = og_image.get('content')
            except:
                pass
            
            # Deterministic Extraction Logic
            try:
                if retailer_name == 'Amazon':
                    # Check for bot/captcha
                    if 'robot check' in soup.text.lower() or 'enter the characters you see below' in soup.text.lower():
                        print(f"⚠️ [Blocked] Amazon: Encountered CAPTCHA/Bot Check page.")
                        return None
                        
                    # Check for out of stock first
                    availability_elem = soup.select_one('#availability')
                    if availability_elem and ('currently unavailable' in availability_elem.text.lower() or 'out of stock' in availability_elem.text.lower()):
                        print(f"⚠️ [Out of Stock] Amazon: Item is marked unavailable in BS4.")
                        return None
                        
                    # Main price MUST come from the actual new-item buybox first.
                    price_container = soup.select_one('#corePriceDisplay_desktop_feature_div .a-price') or \
                                      soup.select_one('#corePrice_feature_div .a-price') or \
                                      soup.select_one('#corePrice_desktop .a-price') or \
                                      soup.select_one('#apex_desktop .a-price')

                    if not price_container:
                        used_price_container = soup.select_one('#usedBuySection .a-price') or \
                                               soup.select_one('#olp-upd-new-used .a-color-price')
                        if used_price_container:
                            price_container = used_price_container
                            is_refurbished = True

                    if price_container:
                        offscreen = price_container.select_one('.a-offscreen')
                        if offscreen:
                            m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', offscreen.text)
                            if m:
                                try: price = float(m.group(1).replace(',', ''))
                                except ValueError: pass
                        if not price:
                            price_whole = price_container.select_one('.a-price-whole')
                            price_fraction = price_container.select_one('.a-price-fraction')
                            if price_whole:
                                price_str = price_whole.text.replace(',', '').replace('.', '').strip()
                                frac = price_fraction.text.strip() if price_fraction else "00"
                                try: price = float(f"{price_str}.{frac}")
                                except ValueError: pass
                            
                    title_elem = soup.select_one('#productTitle')
                    if title_elem: title = title_elem.text.strip()
                    
                    # Reject out of stock items explicitly
                    if availability_elem and ('currently unavailable' in availability_elem.text.lower() or 'out of stock' in availability_elem.text.lower()):
                        print(f"⚠️ [Out of Stock] Amazon: Currently unavailable.")
                        return None
                    
                elif retailer_name == 'Newegg':
                    # Extract main price from active buy box
                    price_elem = soup.select_one('.price-current') or soup.select_one('[class^="price-current"]')
                    if price_elem:
                        price_strong = price_elem.select_one('strong')
                        price_sup = price_elem.select_one('sup')
                        if price_strong:
                            price_str = price_strong.text.replace(',', '').strip()
                            frac = price_sup.text.strip() if price_sup else ".00"
                            try: price = float(f"{price_str}{frac}")
                            except ValueError: pass
                        else:
                            m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                            if m:
                                try: price = float(m.group(1).replace(',', ''))
                                except ValueError: pass
                            
                    title_elem = soup.select_one('.product-title') or soup.select_one('h1.product-title')
                    if title_elem: title = title_elem.text.strip()
                    
                    # Reject out of stock items explicitly
                    inventory_elem = soup.select_one('.product-inventory')
                    if inventory_elem and 'out of stock' in inventory_elem.text.lower():
                        print(f"⚠️ [Out of Stock] Newegg: Out of stock.")
                        return None
                    
                elif retailer_name == 'Best Buy':
                    # 1. Try JSON-LD first for highly accurate pricing
                    for script in soup.find_all('script', type='application/ld+json'):
                        if not script.string: continue
                        try:
                            data = __import__('json').loads(script.string)
                            items = data if isinstance(data, list) else [data]
                            for item in items:
                                if item.get('@type') == 'Product' and 'offers' in item:
                                    item_name = item.get('name', '').lower()
                                    if model_query and item_name:
                                        clean_q_words = [w for w in re.sub(r'[^a-z0-9\s]', '', model_query.lower()).split() if len(w) > 2]
                                        digit_tokens = [w for w in clean_q_words if re.search(r'\d', w)]
                                        clean_item_name = re.sub(r'[^a-z0-9]', '', item_name)
                                        if digit_tokens and not all(d in clean_item_name for d in digit_tokens):
                                            continue
                                            
                                    offers = item['offers']
                                    if isinstance(offers, dict) and 'price' in offers:
                                        price = float(offers['price'])
                                        break
                                    elif isinstance(offers, list) and len(offers) > 0 and 'price' in offers[0]:
                                        price = float(offers[0]['price'])
                                        break
                            if price: break
                        except: pass
                    
                    # 2. Fallback to expanded CSS selectors
                    if not price:
                        price_candidates = soup.select(
                            '.priceView-customer-price span[aria-hidden="true"], '
                            '.priceView-hero-price span[aria-hidden="true"], '
                            'div[data-testid="customer-price"] span[aria-hidden="true"], '
                            'div[data-testid="price-block-customer-price"] span[aria-hidden="true"], '
                            '.pricing-price span'
                        )
                        for p in price_candidates:
                            m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', p.text)
                            if m:
                                try:
                                    price = float(m.group(1).replace(',', ''))
                                    break
                                except ValueError: continue
                            
                    title_elem = soup.select_one('.sku-title h1, h1[class*="product-title"], h1.heading-5, h1[class*="text-5"]')
                    if title_elem: title = title_elem.text.strip()
                    
                    # Reject out of stock items explicitly
                    add_to_cart_btn = soup.select_one('.add-to-cart-button, button[data-button-state]')
                    if add_to_cart_btn and ('sold_out' in add_to_cart_btn.get('data-button-state', '').lower() or 'sold out' in add_to_cart_btn.text.lower()):
                        print(f"⚠️ [Out of Stock] Best Buy: Sold out.")
                        return None
                    
                elif retailer_name == 'B&H':
                    price_elem = soup.select_one('[data-selenium="pricingPrice"]') or \
                                 soup.select_one('.price__9gLfjPSjp') or \
                                 soup.select_one('[data-selenium="pricingContainer"] .price__9gLfjPSjp')
                    if price_elem:
                        m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', price_elem.text)
                        if m:
                            try: price = float(m.group(1).replace(',', ''))
                            except ValueError: pass
                    title_elem = soup.select_one('[data-selenium="productTitle"]') or soup.select_one('h1[data-selenium="productTitle"]')
                    if title_elem: title = title_elem.text.strip()
                    
                    # Reject out of stock / no longer available explicitly
                    availability_elem = soup.select_one('.shippingAvail_yL7x0I4P, [data-selenium="stockStatus"]')
                    if availability_elem and ('no longer available' in availability_elem.text.lower() or 'discontinued' in availability_elem.text.lower()):
                        print(f"⚠️ [Out of Stock] B&H: No longer available.")
                        return None
                    
                elif retailer_name == 'eBay':
                    # Extract main price from eBay buy box (ignore strikethrough list prices and financing)
                    price_elem = soup.select_one('.x-price-primary') or soup.select_one('[data-testid="x-price-primary"]') or soup.select_one('.x-price-approx')
                    if price_elem:
                        spans = price_elem.select('.ux-textspans')
                        valid_spans = [s for s in spans if 'strikethrough' not in ''.join(s.get('class', [])).lower()]
                        target_text = valid_spans[0].text if valid_spans else price_elem.text
                        m = re.search(r'\$([0-9,]+(?:\.[0-9]{2})?)', target_text)
                        if m:
                            try: price = float(m.group(1).replace(',', ''))
                            except ValueError: pass
                    
                    # Extract title
                    title_elem = soup.select_one('.x-item-title__mainTitle span.ux-textspans') or soup.select_one('.x-item-title__mainTitle')
                    if title_elem: title = title_elem.text.strip()
                    
                    # Reject ended/out of stock items explicitly
                    ended_msg = soup.select_one('.x-ended-item-msg, .msg-error, .ux-notice')
                    if ended_msg and ('ended' in ended_msg.text.lower() or 'out of stock' in ended_msg.text.lower()):
                        print(f"⚠️ [Out of Stock] eBay: Listing ended or out of stock.")
                        return None
                    
                    # Extract condition (Used vs New) to set is_refurbished roughly
                    condition_elem = soup.select_one('.x-item-condition-text span.ux-textspans') or soup.select_one('.x-item-condition-text')
                    if condition_elem:
                        cond_text = condition_elem.text.lower()
                        if 'used' in cond_text or 'refurbished' in cond_text or 'parts' in cond_text or 'as is' in cond_text:
                            is_refurbished = True
                            
                        # Reject broken items instantly
                        if 'for parts' in cond_text or 'not working' in cond_text or 'box only' in cond_text or 'as is' in cond_text:
                            print(f"⚠️ [Rejected] eBay: Item is marked as broken/parts ({condition_elem.text}).")
                            return None
                            
                elif retailer_name == 'Micro Center':
                    # Extract price
                    price = 0
                    # Try OpenGraph price first (Micro Center often puts it here)
                    og_price = soup.select_one('meta[property="og:price:amount"]') or soup.select_one('meta[itemprop="price"]')
                    if og_price and og_price.get('content'):
                        m = re.search(r'([0-9,]+(?:\.[0-9]{2})?)', og_price['content'])
                        if m:
                            try: price = float(m.group(1).replace(',', ''))
                            except ValueError: pass
                    
                    if not price:
                        price_elem = soup.select_one('#pricing') or soup.select_one('#pricing2')
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
                    
                    # Extract title
                    title_elem = soup.select_one('.ProductLink_' + soup.select_one('[data-id]')['data-id']) if soup.select_one('[data-id]') else None
                    if not title_elem:
                        title_elem = soup.select_one('[data-name]')
                        
                    if title_elem and title_elem.get('data-name'):
                        title = title_elem['data-name'].strip()
                    elif title_elem:
                        title = title_elem.text.strip()
                        
                    # Check stock status (ONLY in the specific inventory element to avoid false positives)
                    inventory_elem = soup.select_one('.inventoryCnt, .out-of-stock, .unavailable, .availabilityTrunc')
                    if inventory_elem:
                        inventory_text = inventory_elem.text.lower()
                        if 'sold out' in inventory_text or 'no longer carried' in inventory_text or 'not available' in inventory_text:
                            print(f"⚠️ [Out of Stock] Micro Center: Sold out or no longer carried.")
                            return None
            except Exception as e:
                print(f"[BS4 Parse Error] {retailer_name}: {e}")
                
            # If title is extracted, we can do a single-item check if not already performed via batch
            if title and model_query:
                # Include URL text in the match check so retailer-specific product titles that omit brand headers (e.g. Micro Center) still match
                full_check_title = f"{title} {url}"
                is_match = (await self.filter_matching_titles([full_check_title], model_query, category))[0]
                if not is_match:
                    print(f"⚠️ [Title Mismatch] {retailer_name}: \"{title}\" does not match query \"{model_query}\"")
                    return None
            elif model_query:
                # No title could be scraped (selector drift, unusual page layout, etc). The old
                # code fell straight through to accepting whatever price it found, with zero
                # verification that the page was even for the right product. Validate against
                # a slice of the raw page text instead of blindly trusting the price.
                page_text_sample = (markdown_content or html_content or '')[:3000]
                is_match = (await self.filter_matching_titles([f"{page_text_sample} {url}"], model_query, category))[0]
                if not is_match:
                    print(f"⚠️ [No Title — Content Mismatch] {retailer_name}: page content doesn't confirm \"{model_query}\"")
                    return None
                
            if price and self.is_price_sanity_valid(price, model_query or title or url, category):
                print(f"✅ [BS4 Hit] {retailer_name}: Found price ${price:.2f}")
                return {
                    "retailer": retailer_name,
                    "price": price,
                    "originalPrice": None,
                    "title": title or model_query or url,
                    "brand": None,
                    "url": url,
                    "imageUrl": image_url,
                    "inStock": in_stock,
                    "isRefurbished": is_refurbished
                }
                
            print(f"⚠️ [BS4 Miss] {retailer_name}: Falling back to AI extraction...")
            groq_data = await self.parse_with_groq(markdown_content, model_query, retailer_name, category)
            if groq_data and groq_data.get('price'):
                if self.is_price_sanity_valid(groq_data['price'], model_query or title or url, category):
                    print(f"✅ [Groq Fallback Hit] {retailer_name}: Found price ${groq_data['price']:.2f}")
                    return {
                        "retailer": retailer_name,
                        "price": groq_data['price'],
                        "originalPrice": groq_data.get('originalPrice'),
                        "title": groq_data.get('title') or title or model_query or url,
                        "brand": groq_data.get('brand'),
                        "url": url,
                        "imageUrl": image_url,
                        "inStock": groq_data.get('inStock', in_stock),
                        "isRefurbished": groq_data.get('isRefurbished', is_refurbished)
                    }
                else:
                    print(f"⚠️ [Groq Hallucination] {retailer_name}: Extracted price ${groq_data['price']:.2f} failed sanity check.")
                    
            return None
        except Exception as e:
            print(f"⚠️ [Firecrawl Extraction Error] {url}: {e}")
            return None

    async def parse_with_groq(self, markdown_content: str, query: str, retailer: str, category: str) -> dict:
        if not GROQ_API_KEY:
            return {}

        # Strip distracting recommended carousel headers and sidebars
        clean_markdown = re.sub(r'##\s*People who viewed this item also viewed[\s\S]*?(?=##|\Z)', '', markdown_content, flags=re.I)
        clean_markdown = re.sub(r'##\s*Similar items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)
        clean_markdown = re.sub(r'##\s*Sponsored items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)
        clean_markdown = re.sub(r'##\s*Compare with similar items[\s\S]*?(?=##|\Z)', '', clean_markdown, flags=re.I)

        system_prompt = (
            f"You are an expert product data extraction agent parsing a {retailer} product page. "
            f"Extract product details for the target item: '{query}' ({category}). "
            "CRITICAL RULES:\n"
            "1. Extract the current active BUYBOX selling price for the MAIN product.\n"
            "2. NEVER extract protection plans (e.g. '$28.00'), warranties, financing (e.g. '$55/mo'), or shipping fees.\n"
            "3. NEVER extract prices of related or recommended items from carousels.\n"
            "4. Return ONLY valid JSON with keys: price (float), originalPrice (float or null), title (str), brand (str or null), inStock (bool)."
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
                    print(f"[Groq 429] Rate limit on AI fallback — skipping {retailer}")
        except Exception as e:
            print(f"[Groq Price Parse Error] {e}")

        return {}

    def is_valid_direct_product_url(self, url: str, domain_pattern: str) -> bool:
        lower = url.lower()
        if any(x in lower for x in ['/reviews/', 'reviews', 'questions', '/forum/', '/blog/', 'searchpage.jsp', '/s?k=', '/p/pl']):
            return False
            
        if 'microcenter.com' in domain_pattern: return '/product/' in lower
        if 'amazon.com' in domain_pattern: return '/dp/' in lower or '/gp/product/' in lower
        if 'newegg.com' in domain_pattern: return '/p/' in lower and not '/p/pl' in lower
        if 'bestbuy.com' in domain_pattern: return ('/site/' in lower and '.p' in lower) or '/product/' in lower
        if 'bhphotovideo.com' in domain_pattern: return '/c/product/' in lower and not '/accessories' in lower
        if 'ebay.com' in domain_pattern: return '/itm/' in lower
        return True

    async def filter_matching_titles(self, titles: list[str], query: str, category: str) -> list[bool]:
        results = []
        
        # 1. Smart tokenize query (split glued words, camelCase, and punctuation)
        q_raw = re.sub(r'(?i)\bwi-?fi\b', 'wifi', query)
        q_raw = re.sub(r'(?i)\bmini-?itx\b', 'itx', q_raw)
        q_raw = re.sub(r'(?i)\bmicro-?atx\b', 'matx', q_raw)
        q_raw = re.sub(r'(?i)\bgen\s*([345])\b', r'gen\1', q_raw)
        q_raw = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', q_raw)
        q_raw = re.sub(r'([a-zA-Z]{2,})(\d+)', r'\1 \2', q_raw)
        q_raw = re.sub(r'(\d+)([a-zA-Z]{2,})', r'\1 \2', q_raw)
        q_tokens = [w for w in re.sub(r'[^a-z0-9\s]', ' ', q_raw.lower()).split() if len(w) > 0]
        lower_q = query.lower()
        
        # Extract model digits (e.g. '890', '4070', '7800', '9900', '265', '270', '850', '2', '64')
        digit_tokens = [w for w in q_tokens if re.search(r'\d', w)]
        pure_digits = [re.findall(r'\d+', w)[0] for w in digit_tokens if re.findall(r'\d+', w)]
        
        # Comprehensive dictionary of hardware & peripheral brands
        BRANDS = {
            'asrock', 'asus', 'msi', 'gigabyte', 'zotac', 'pny', 'sapphire', 'xfx', 'evga', 'powercolor', 'inno3d', 'palit', 'gainward',
            'corsair', 'samsung', 'intel', 'amd', 'nvidia', 'crucial', 'gskill', 'g.skill', 'kingston', 'teamgroup', 'patriot', 'adata', 'xpg',
            'nzxt', 'thermalright', 'be quiet', 'bequiet', 'noctua', 'arctic', 'deepcool', 'id-cooling', 'phanteks', 'lian li', 'lianli', 'fractal', 'montech', 'hyte', 'antec', 'cooler master', 'coolermaster', 'thermaltake', 'silverstone', 'seasonic', 'super flower',
            'western digital', 'wd', 'seagate', 'sabrent', 'sk hynix', 'solidigm', 'kioxia',
            'logitech', 'razer', 'steelseries', 'hyperx', 'corsair', 'glorious', 'wooting', 'keychron', 'ducky', 'epomaker', 'varmilo', 'akko', 'rk royal kludge', 'redragon', 'finalmouse', 'pulsar', 'lamzu', 'ninjutso', 'endgame gear', 'vaxee', 'zowie',
            'elgato', 'shure', 'rode', 'audio-technica', 'sennheiser', 'beyerdynamic', 'fifine', 'blue',
            'lg', 'dell', 'alienware', 'samsung', 'acer', 'aoc', 'benq', 'viewsonic', 'innocn', 'ktc'
        }
        query_brands = [w for w in q_tokens if w in BRANDS]
        
        # Significant distinguishing model words & modifiers
        CRITICAL_MODIFIERS = {'plus', 'super', 'ti', 'xt', 'xtx', 'wifi', 'white', 'liquid', 'water', 'wireless', 'rgb', 'argb', 'oled', 'lcd', 'heatsink', 'modular', 'sfx', 'itx', 'matx', 'atx'}
        query_modifiers = [w for w in q_tokens if w in CRITICAL_MODIFIERS]
        
        for t in titles:
            lower_t = t.lower()
            clean_t = re.sub(r'[^a-z0-9]', '', lower_t)
            
            # Rule 1: Reject junk/noise unless explicitly requested
            bad_keywords = [
                'for parts', 'broken', 'box only', 'read description', 'empty box', 
                'sticker only', 'packaging only', 'manual only', 'dummy', 'poster', 'as is', 'case badge'
            ]
            if any(b in lower_t and b not in lower_q for b in bad_keywords):
                results.append(False)
                continue

            # Rule 2: Reject prebuilt PCs when looking for individual components
            if category in ['GPU', 'CPU', 'Motherboard', 'RAM', 'Storage', 'Power Supply', 'Case', 'Cooling']:
                if not any(k in lower_q for k in ['pc', 'desktop', 'prebuilt', 'laptop', 'system']):
                    if any(p in lower_t for p in ['desktop pc', 'gaming pc', 'gaming desktop', 'laptop', 'prebuilt pc', 'complete pc', 'all-in-one']):
                        results.append(False)
                        continue
                        
            # Rule 3: Reject combos/bundles when looking for standalone components
            if category in ['GPU', 'CPU', 'Motherboard', 'RAM', 'Storage', 'Power Supply', 'Case', 'Cooling']:
                if not any(k in lower_q for k in ['combo', 'bundle', 'pack', 'set']):
                    if any(b in lower_t for b in ['motherboard combo', 'cpu combo', 'with motherboard', '+ motherboard', 'plus motherboard', 'cpu + mobo', 'gpu + mobo', 'cpu motherboard combo']):
                        results.append(False)
                        continue

            # Rule 4: Must contain all core numeric sequences (e.g. '890' for Z890, '4070', '7800', '265', '990', '850')
            if pure_digits and not all(d in clean_t for d in pure_digits):
                results.append(False)
                continue
                
            # Rule 5: If query specifies a brand (e.g. 'asrock', 'samsung'), title must belong to that brand
            if query_brands:
                if not any(b.replace(' ', '').replace('.', '') in clean_t for b in query_brands):
                    results.append(False)
                    continue
                
            # Rule 6: If query specifies a critical modifier (e.g. 'wifi', 'super', 'ti', 'white', 'wireless'), title must contain it
            if query_modifiers:
                if not all(m in clean_t for m in query_modifiers):
                    results.append(False)
                    continue
                    
            results.append(True)
            
        return results

    def is_price_sanity_valid(self, price: float, query: str, category: str) -> bool:
        if not price or price <= 0:
            return False

        # Category-level realistic price floors & ceilings for modern computer hardware
        CATEGORY_PRICE_BOUNDS = {
            'GPU': (50.0, 5000.0),
            'CPU': (35.0, 3000.0),
            'Motherboard': (35.0, 1500.0),
            'RAM': (15.0, 1500.0),
            'Storage': (15.0, 2000.0),
            'Power Supply': (25.0, 1200.0),
            'Case': (20.0, 1000.0),
            'Cooling': (5.0, 800.0),
            'Case Fans': (5.0, 300.0),
            'Thermal Paste': (3.0, 100.0),
            'Monitor': (40.0, 4000.0),
            'Peripherals': (5.0, 1200.0),
            'Keyboard': (10.0, 600.0),
            'Mouse': (10.0, 300.0),
            'Headset': (10.0, 1000.0),
            'Microphone': (15.0, 800.0),
            'Accessories': (3.0, 500.0),
            'Networking': (10.0, 1500.0),
        }

        min_bound, max_bound = CATEGORY_PRICE_BOUNDS.get(category, (3.0, 10000.0))
        if price < min_bound or price > max_bound:
            return False

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
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": "groq/compound-mini",
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
            print(f"[Groq Category Detect Error] {e}")
            pass
            
        return 'Not compatible (N/A)'

    def normalize_model(self, text: str, fallback: str) -> str:
        if fallback and len(fallback.strip()) > 2:
            return fallback.strip()
        clean = re.sub(r'^(asus|msi|gigabyte|zotac|evga|sapphire|xfx|pny|powercolor|asrock|intel|amd|nvidia|corsair|g\.skill|samsung|crucial|western digital|wd)\s+', '', text, flags=re.I)
        clean = re.sub(r'\s+(graphics card|video card|processor|desktop processor|cpu|gpu|ddr4|ddr5|ram|nvme|ssd|motherboard|power supply|edition|oc|gaming).*$', '', clean, flags=re.I).strip()
        return clean or text
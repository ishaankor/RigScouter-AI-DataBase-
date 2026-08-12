import os
import json
import time
import requests
from datetime import datetime, timezone
import random
import re
from bs4 import BeautifulSoup
from firecrawl import FirecrawlApp

from supabase_client import supabase

TAVILY_API_KEYS = [k.strip() for k in os.environ.get("TAVILY_API_KEYS", os.environ.get("TAVILY_API_KEY", "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")).split(',') if k.strip()]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

firecrawl_app = FirecrawlApp(api_key=FIRECRAWL_API_KEY) if FIRECRAWL_API_KEY else None

class TavilyHardwareAgent:
    def __init__(self):
        self.current_key_index = 0

    def get_tavily_key(self):
        return TAVILY_API_KEYS[self.current_key_index % len(TAVILY_API_KEYS)]

    def rotate_tavily_key(self):
        if len(TAVILY_API_KEYS) > 1:
            self.current_key_index = (self.current_key_index + 1) % len(TAVILY_API_KEYS)
            print(f"[Tavily Key Rotated] Active key index: {self.current_key_index + 1}/{len(TAVILY_API_KEYS)}")

    async def run(self, prompt: str, emit_fn=None) -> dict:
        clean_prompt = prompt.strip()
        is_url = clean_prompt.startswith('http://') or clean_prompt.startswith('https://')
        category = self.detect_category(clean_prompt)

        print(f"\n======================================================")
        print(f"[Hybrid Python Agent] Extracting price for: \"{clean_prompt}\" ({category})")
        print(f"======================================================\n")

        if emit_fn:
            emit_fn('agent_start', {'query': clean_prompt, 'category': category, 'timestamp': datetime.now(timezone.utc).isoformat()})

        state = {
            "userQuery": clean_prompt,
            "category": category,
            "scrapedOffers": [],
            "summary": ""
        }

        if category == 'Not compatible (N/A)' and not is_url:
            print(f"[Non-PC Part Query Rejected] \"{clean_prompt}\" is Not compatible (N/A)")
            state["summary"] = f"Not compatible (N/A) — \"{clean_prompt}\" is not a recognized PC hardware component."
            if emit_fn:
                emit_fn('agent_complete', {
                    "query": clean_prompt,
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
                {'name': 'B&H', 'domain': 'bhphotovideo.com'}
            ]

            for r in RETAILERS:
                offer = await self.scrape_retailer_accurate_offer(clean_prompt, r['name'], r['domain'], category)
                if offer and offer.get('price', 0) > 0:
                    state["scrapedOffers"].append(offer)
                    if emit_fn:
                        emit_fn('retailer_found', {
                            "query": clean_prompt,
                            "retailer": offer['retailer'],
                            "price": offer['price'],
                            "title": offer['title'],
                            "url": offer['url'],
                            "inStock": offer['inStock'],
                            "isRefurbished": offer.get('isRefurbished', False),
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                time.sleep(1)  # Rate limiting

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

        if state.get("bestOffer"):
            try:
                res = supabase.table('hardware_components').select('current_price').ilike('model', f"%{clean_prompt}%").order('updated_at', desc=True).limit(1).execute()
                previous_price = res.data[0]['current_price'] if res.data else None

                if previous_price is not None:
                    diff = state["bestOffer"]['price'] - previous_price
                    if diff < -0.5:
                        state["priceChange"] = 'drop'
                        state["previousPrice"] = previous_price
                        if emit_fn:
                            emit_fn('price_drop', {
                                "query": clean_prompt,
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

                for offer in state["scrapedOffers"]:
                    offer_model_group = self.normalize_model(offer['title'], clean_prompt)
                    offer_component_id = f"comp-{re.sub(r'[^a-z0-9]+', '-', offer_model_group.lower())}-{re.sub(r'[^a-z0-9]+', '-', offer['retailer'].lower())}"

                    offer_msrp = offer.get('originalPrice') if offer.get('originalPrice') and offer.get('originalPrice') > offer['price'] else offer['price']
                    
                    offer_deal_score = min(100, max(50, round(50 + ((offer_msrp - offer['price']) / offer_msrp) * 100))) if offer_msrp > offer['price'] else 50

                    supabase.table('hardware_components').upsert({
                        "id": offer_component_id,
                        "name": offer['title'],
                        "category": category,
                        "brand": offer.get('brand') or offer['title'].split(' ')[0] or 'Hardware',
                        "model": offer_model_group,
                        "specs": json.dumps({
                            "AgentSummary": state["summary"],
                            "RetailerOffers": state["scrapedOffers"],
                            "InStock": offer['inStock'],
                            "IsRefurbished": offer.get('isRefurbished', False),
                            "OriginalPrice": offer.get('originalPrice'),
                            "PreviousPrice": previous_price,
                            "PriceChange": state.get("priceChange"),
                            "ScrapedAt": datetime.now(timezone.utc).isoformat()
                        }),
                        "msrp": offer_msrp,
                        "current_price": offer['price'],
                        "lowest_price_90d": offer['price'],
                        "retailer": offer['retailer'],
                        "product_url": offer['url'],
                        "image_url": offer.get('imageUrl', 'https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=600&q=80'),
                        "rating": offer.get('rating'),
                        "deal_score": offer_deal_score,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).execute()
                    
                    print(f"[DB Persist Success] Saved \"{offer['retailer']}\" offer: \"{offer['title']}\" (${offer['price']:.2f}) to hardware_components")
            except Exception as e:
                print(f"[Agent Persistence Error]: {e}")

        if emit_fn:
            emit_fn('agent_complete', {
                "query": clean_prompt,
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

    async def scrape_retailer_accurate_offer(self, model_query: str, retailer_name: str, domain_pattern: str, category: str) -> dict:
        print(f"[Tavily Search] Querying {retailer_name} for \"{model_query}\" ({category})...")
        
        for attempt in range(len(TAVILY_API_KEYS)):
            try:
                # 1. Search with Tavily to get URLs (NO raw_content)
                res = requests.post('https://api.tavily.com/search', json={
                    "api_key": self.get_tavily_key(),
                    "query": f"buy {model_query} price",
                    "search_depth": "advanced",
                    "include_domains": [domain_pattern],
                    "include_raw_content": False,
                    "max_results": 3
                }, timeout=15)
                
                if not res.ok:
                    raise Exception(f"Tavily search failed: {res.text}")
                    
                data = res.json()
                valid_hits = []
                
                for hit in data.get('results', []):
                    full_url = hit.get('url', '')
                    if not self.is_valid_direct_product_url(full_url, domain_pattern):
                        continue
                        
                    clean_url = full_url.replace('/reviews/', '').split('?')[0]
                    content = hit.get('content', '')
                    
                    # Estimate price from snippet
                    prices = re.findall(r'\$[0-9,]+(?:\.[0-9]{2})?', content)
                    est_price = float('inf')
                    if prices:
                        try:
                            parsed_prices = [float(p.replace('$', '').replace(',', '')) for p in prices]
                            valid_prices = [p for p in parsed_prices if self.is_price_sanity_valid(p, model_query, category)]
                            if valid_prices:
                                est_price = min(valid_prices)
                        except:
                            pass
                            
                    valid_hits.append({
                        'url': clean_url,
                        'est_price': est_price
                    })

                if not valid_hits:
                    return None
                    
                # Sort by estimated price (putting float('inf') at the end)
                valid_hits.sort(key=lambda x: x['est_price'])
                
                # Scrape only the single best candidate to save Firecrawl credits
                best_candidate = valid_hits[0]
                offer = await self.firecrawl_extract(best_candidate['url'], retailer_name, category, model_query)
                if offer:
                    print(f"✅ [CHEAPEST AVAILABLE {retailer_name.upper()} OFFER] \"${offer['price']}\" -> {offer['title'][:60]}")
                    return offer
                    
                # If first candidate fails extraction, try second
                if len(valid_hits) > 1:
                    offer = await self.firecrawl_extract(valid_hits[1]['url'], retailer_name, category, model_query)
                    if offer:
                        print(f"✅ [CHEAPEST AVAILABLE {retailer_name.upper()} OFFER] \"${offer['price']}\" -> {offer['title'][:60]}")
                        return offer
                    
                return None
            except Exception as e:
                print(f"[Tavily Search Error] {retailer_name}: {e}")
                self.rotate_tavily_key()
        return None

    async def firecrawl_extract(self, url: str, retailer_name: str, category: str, model_query: str = None) -> dict:
        if not firecrawl_app:
            print("[Firecrawl] API key missing")
            return None
            
        print(f"[Firecrawl] Extracting {url} ...")
        try:
            # Fetch both HTML (for deterministic BS4) and Markdown (for Groq fallback)
            res = firecrawl_app.scrape_url(url, formats=['html', 'markdown'])
            if not res or not res.get('html'):
                return None
                
            html_content = res.get('html', '')
            markdown_content = res.get('markdown', '')
            
            soup = BeautifulSoup(html_content, 'lxml')
            price = None
            title = None
            in_stock = True
            
            # Deterministic Extraction Logic
            try:
                if retailer_name == 'Amazon':
                    price_whole = soup.select_one('.a-price-whole')
                    price_fraction = soup.select_one('.a-price-fraction')
                    if price_whole:
                        price_str = price_whole.text.replace(',', '').replace('.', '').strip()
                        frac = price_fraction.text.strip() if price_fraction else "00"
                        price = float(f"{price_str}.{frac}")
                    title_elem = soup.select_one('#productTitle')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'Newegg':
                    price_current = soup.select_one('.price-current strong')
                    price_sup = soup.select_one('.price-current sup')
                    if price_current:
                        price_str = price_current.text.replace(',', '').strip()
                        frac = price_sup.text.strip() if price_sup else ".00"
                        price = float(f"{price_str}{frac}")
                    title_elem = soup.select_one('.product-title')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'Micro Center':
                    price_elem = soup.select_one('span[itemprop="price"]') or soup.select_one('.price')
                    if price_elem:
                        price = float(price_elem.text.replace('$', '').replace(',', '').strip())
                    title_elem = soup.select_one('h1 span[data-name="default"]') or soup.select_one('h1')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'Best Buy':
                    price_elem = soup.select_one('.priceView-customer-price span[aria-hidden="true"]')
                    if price_elem:
                        price = float(price_elem.text.replace('$', '').replace(',', '').strip())
                    title_elem = soup.select_one('.sku-title h1')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'B&H':
                    price_elem = soup.select_one('[data-selenium="pricingPrice"]')
                    if price_elem:
                        price = float(price_elem.text.replace('$', '').replace(',', '').strip())
                    title_elem = soup.select_one('[data-selenium="productTitle"]')
                    if title_elem: title = title_elem.text.strip()
            except Exception as e:
                print(f"[BS4 Parse Error] {retailer_name}: {e}")
                
            if price and self.is_price_sanity_valid(price, model_query or title or url, category):
                print(f"✅ [BS4 Hit] {retailer_name}: Found price ${price:.2f}")
                return {
                    "retailer": retailer_name,
                    "price": price,
                    "originalPrice": None,
                    "title": title or model_query or url,
                    "brand": None,
                    "url": url,
                    "inStock": in_stock,
                    "isRefurbished": False
                }
                
            print(f"⚠️ [BS4 Miss] {retailer_name}: Could not find price deterministically. Falling back to Groq LLM...")
            parsed = await self.parse_with_groq(markdown_content[:8000], model_query or url, retailer_name, category)
            if parsed and parsed.get('price') and self.is_price_sanity_valid(parsed['price'], model_query or url, category):
                return {
                    "retailer": retailer_name,
                    "price": parsed['price'],
                    "originalPrice": parsed.get('originalPrice'),
                    "title": parsed.get('title') or model_query or url,
                    "brand": parsed.get('brand'),
                    "url": url,
                    "inStock": parsed.get('inStock', True),
                    "isRefurbished": parsed.get('isRefurbished', False),
                }
                
            return None
        except Exception as e:
            print(f"[Firecrawl Error] {e}")
            return None


    def is_valid_direct_product_url(self, url: str, domain_pattern: str) -> bool:
        lower = url.lower()
        if any(x in lower for x in ['/reviews/', '/forum/', '/blog/', 'searchpage.jsp', '/s?k=', '/p/pl']):
            return False
        if 'microcenter.com' in domain_pattern: return '/product/' in lower
        if 'amazon.com' in domain_pattern: return '/dp/' in lower or '/gp/product/' in lower
        if 'newegg.com' in domain_pattern: return '/p/' in lower and not '/p/pl' in lower
        if 'bestbuy.com' in domain_pattern: return '/site/' in lower and '.p?' in lower
        if 'bhphotovideo.com' in domain_pattern: return '/c/product/' in lower and not '/accessories' in lower
        return True

    def is_price_sanity_valid(self, price: float, query: str, category: str) -> bool:
        if not price or price <= 0: return False
        lower = query.lower()
        if '4070 super' in lower: return 450 <= price <= 950
        if category == 'GPU' and (price < 80 or price > 5000): return False
        return True

    def detect_retailer(self, text: str) -> str:
        lower = text.lower()
        if 'microcenter' in lower: return 'Micro Center'
        if 'newegg' in lower: return 'Newegg'
        if 'bestbuy' in lower: return 'Best Buy'
        if 'bhphotovideo' in lower: return 'B&H'
        return 'Amazon'

    def detect_category(self, text: str) -> str:
        lower = text.lower()
        if any(x in lower for x in ['rtx', 'gtx', 'radeon', 'rx', 'gpu']): return 'GPU'
        if any(x in lower for x in ['ryzen', 'core', 'cpu']): return 'CPU'
        if any(x in lower for x in ['ram', 'ddr']): return 'RAM'
        return 'Hardware'

    def normalize_model(self, text: str, fallback: str) -> str:
        lower = text.lower()
        if '4070 super' in lower: return 'RTX 4070 Super'
        if '7800x3d' in lower: return 'Ryzen 7 7800X3D'
        return fallback or text

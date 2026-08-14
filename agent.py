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
FIRECRAWL_API_KEYS = [k.strip() for k in os.environ.get("FIRECRAWL_API_KEYS", os.environ.get("FIRECRAWL_API_KEY", "")).split(',') if k.strip()]
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

firecrawl_apps = [FirecrawlApp(api_key=key) for key in FIRECRAWL_API_KEYS] if FIRECRAWL_API_KEYS else []

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
                {'name': 'B&H', 'domain': 'bhphotovideo.com'},
                {'name': 'eBay', 'domain': 'ebay.com'}
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
                    "max_results": 10
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
                    title = hit.get('title', '')
                    
                    # Pre-filter using Tavily's returned title to save Firecrawl credits
                    if title and not await self.is_title_match(title, model_query, category):
                        continue
                    
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
                
                # Loop through all valid hits until we extract a successful, matching offer
                for candidate in valid_hits:
                    offer = await self.firecrawl_extract(candidate['url'], retailer_name, category, model_query)
                    if offer:
                        print(f"✅ [CHEAPEST AVAILABLE {retailer_name.upper()} OFFER] \"${offer['price']}\" -> {offer['title'][:60]}")
                        return offer
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
        try:
            try:
                res = app.scrape_url(url, formats=['html', 'markdown'])
            except Exception as e:
                if "Payment" in str(e) or "credits" in str(e) or "401" in str(e) or "402" in str(e):
                    print(f"[Firecrawl Error] Credit limit reached: {e}")
                    self.rotate_firecrawl_key()
                    app = self.get_firecrawl_app()
                    print(f"[Firecrawl] Retrying {url} with new key ...")
                    res = app.scrape_url(url, formats=['html', 'markdown'])
                else:
                    raise e
            
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
            in_stock = True
            is_refurbished = False
            
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
                        
                    # Main price is usually in specific buy box containers
                    price_container = soup.select_one('#corePriceDisplay_desktop_feature_div .a-price') or \
                                      soup.select_one('#corePrice_feature_div .a-price') or \
                                      soup.select_one('#corePrice_desktop .a-price') or \
                                      soup.select_one('#apex_desktop .a-price')

                    if price_container:
                        price_whole = price_container.select_one('.a-price-whole')
                        price_fraction = price_container.select_one('.a-price-fraction')
                        if price_whole:
                            price_str = price_whole.text.replace(',', '').replace('.', '').strip()
                            frac = price_fraction.text.strip() if price_fraction else "00"
                            price = float(f"{price_str}.{frac}")
                            
                    title_elem = soup.select_one('#productTitle')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'Newegg':
                    # Avoid picking up related/sponsored items by ignoring .item-container
                    main_price_candidates = [p for p in soup.select('.price-current') if not p.find_parent(class_='item-container')]
                    price_container = main_price_candidates[0] if main_price_candidates else soup.select_one('.price-current')
                    
                    if price_container:
                        price_strong = price_container.select_one('strong')
                        price_sup = price_container.select_one('sup')
                        if price_strong:
                            price_str = price_strong.text.replace(',', '').strip()
                            frac = price_sup.text.strip() if price_sup else ".00"
                            price = float(f"{price_str}{frac}")
                            
                    title_elem = soup.select_one('.product-title')
                    if title_elem: title = title_elem.text.strip()
                    
                    
                elif retailer_name == 'Best Buy':
                    # 1. Try JSON-LD first for highly accurate pricing
                    for script in soup.find_all('script', type='application/ld+json'):
                        if not script.string: continue
                        try:
                            data = __import__('json').loads(script.string)
                            items = data if isinstance(data, list) else [data]
                            for item in items:
                                if item.get('@type') == 'Product' and 'offers' in item:
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
                            text = p.text.replace('$', '').replace(',', '').strip()
                            try:
                                if text: 
                                    price = float(text)
                                    break
                            except ValueError: continue
                            
                    title_elem = soup.select_one('.sku-title h1, h1[class*="product-title"], h1.heading-5, h1[class*="text-5"]')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'B&H':
                    price_elem = soup.select_one('[data-selenium="pricingPrice"]')
                    if price_elem:
                        price = float(price_elem.text.replace('$', '').replace(',', '').strip())
                    title_elem = soup.select_one('[data-selenium="productTitle"]')
                    if title_elem: title = title_elem.text.strip()
                    
                elif retailer_name == 'eBay':
                    # Extract main price from eBay buy box
                    price_elem = soup.select_one('.x-price-primary span.ux-textspans') or soup.select_one('.x-price-primary')
                    if price_elem:
                        # e.g., "US $350.00" -> "350.00"
                        price_text = price_elem.text.replace('US', '').replace('$', '').replace(',', '').strip()
                        try: price = float(price_text)
                        except ValueError: pass
                    
                    # Extract title
                    title_elem = soup.select_one('.x-item-title__mainTitle span.ux-textspans') or soup.select_one('.x-item-title__mainTitle')
                    if title_elem: title = title_elem.text.strip()
                    
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
                    price_elem = soup.select_one('#pricing') or soup.select_one('#pricing2')
                    if price_elem and price_elem.get('content'):
                        try: price = float(price_elem['content'])
                        except ValueError: pass
                    
                    # Extract title
                    title_elem = soup.select_one('.ProductLink_' + soup.select_one('[data-id]')['data-id']) if soup.select_one('[data-id]') else None
                    if not title_elem:
                        title_elem = soup.select_one('[data-name]')
                        
                    if title_elem and title_elem.get('data-name'):
                        title = title_elem['data-name'].strip()
                    elif title_elem:
                        title = title_elem.text.strip()
                        
                    # Check stock status
                    inventory_elem = soup.select_one('.inventoryCnt')
                    if inventory_elem and 'sold out' in inventory_elem.text.lower():
                        in_stock = False
            except Exception as e:
                print(f"[BS4 Parse Error] {retailer_name}: {e}")
                
            if title and model_query and not await self.is_title_match(title, model_query, category):
                print(f"⚠️ [Title Mismatch] {retailer_name}: \"{title}\" does not match query \"{model_query}\" (Skipping Groq)")
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
                    "inStock": in_stock,
                    "isRefurbished": is_refurbished
                }
                
            print(f"⚠️ [BS4 Miss] {retailer_name}: Could not find price deterministically. Falling back to Groq LLM...")
            parsed = await self.parse_with_groq(markdown_content[:5000], model_query or url, retailer_name, category)
            if parsed and parsed.get('price') and self.is_price_sanity_valid(parsed['price'], model_query or url, category):
                parsed_title = parsed.get('title') or title or url
                if parsed_title and model_query and not await self.is_title_match(parsed_title, model_query, category):
                    print(f"⚠️ [Title Mismatch] {retailer_name}: \"{parsed_title}\" does not match query \"{model_query}\"")
                    return None
                    
                if parsed.get('inStock') is False:
                    print(f"⚠️ [Out of Stock] {retailer_name}: Item is out of stock (Groq fallback), skipping.")
                    return None
                    
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

    async def parse_with_groq(self, markdown_content: str, query: str, retailer: str, category: str) -> dict:
        if not GROQ_API_KEY:
            print("[Groq API] Key missing.")
            return None
            
        system_prompt = (
            "You are a strict JSON extractor. Given the markdown of a product page, extract the core product details. "
            "Return ONLY valid JSON with keys: price (float, nullable), title (string), brand (string, nullable), "
            "inStock (boolean), originalPrice (float, nullable), isRefurbished (boolean). "
            f"The user was looking for '{query}'. Extract the price of the MAIN product. "
            "CRITICAL RULES:\n"
            "1. Ignore prices for 'frequently bought together', 'sponsored items', or 'related products'.\n"
            "2. If the text says 'Currently unavailable', 'Out of stock', or 'We don't know when or if this item will be back in stock', you MUST set inStock to false and price to null.\n"
            "3. If the retailer is eBay, reject any listing that implies the item is broken, 'For Parts', 'Not Working', 'Box Only', or 'As Is' (set price to null).\n"
            "4. If the item is listed as Used or Refurbished, set isRefurbished to true."
        )
        
        import asyncio
        try:
            for attempt in range(3):
                res = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "llama-3.1-8b-instant",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Extract info from this markdown:\n\n{markdown_content}"}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0
                    },
                    timeout=15
                )
                if res.ok:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    return json.loads(content)
                elif res.status_code == 429:
                    print(f"[Groq 429] Rate limit hit. Waiting 5 seconds before retry...")
                    await asyncio.sleep(5)
                else:
                    print(f"[Groq API Error] {res.status_code} - {res.text}")
                    return None
            return None
        except Exception as e:
            print(f"[Groq Request Error] {e}")
            return None

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

    async def is_title_match(self, title: str, query: str, category: str) -> bool:
        if not title or not query: return True
        
        system_prompt = (
            "You are a strict product matching assistant. The user wants to buy a specific computer part or electronic. "
            "Your job is to determine if the product title matches the requested item. "
            "Return ONLY a JSON object with a single boolean key: 'is_match'. "
            "Rules:\n"
            "1. If the user asks for a part (like a CPU or GPU), reject fully pre-built computers/laptops/systems that just contain the part.\n"
            "2. Reject products listed as 'for parts', 'broken', 'box only', or 'read description'.\n"
            "3. Reject accessories, waterblocks, or unrelated items.\n"
            "4. Minor marketing additions (like 'Desktop Processor', 'Gaming OC') are fine if it's the core product.\n"
            "5. The product must be the EXACT model requested."
        )
        
        user_prompt = f"User searched for: '{query}' (Category: {category})\nProduct Title Found: '{title}'\nDoes this title represent the standalone product being searched for?"
        
        import asyncio
        import json
        import requests
        
        for attempt in range(3):
            try:
                res = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "llama-3.1-8b-instant",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0
                    },
                    timeout=5
                )
                if res.ok:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    return parsed.get('is_match', False)
                elif res.status_code == 429:
                    print(f"[Groq Title Match 429] Rate limit hit. Waiting 5s...")
                    await asyncio.sleep(5)
                    continue
                else:
                    print(f"[Groq Title Match Error]: {res.text}")
                    return True
            except Exception as e:
                print(f"[Groq Title Match Error]: {e}")
                if "rate limit" in str(e).lower() or "429" in str(e):
                    await asyncio.sleep(5)
                    continue
                return True
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
        if 'ebay' in lower: return 'eBay'
        return 'Amazon'

    def detect_category(self, text: str) -> str:
        lower = text.lower()
        if any(x in lower for x in ['rtx', 'gtx', 'radeon', 'rx', 'gpu']): return 'GPU'
        if any(x in lower for x in ['ryzen', 'core', 'cpu', 'threadripper', 'epyc', 'intel core', 'amd ryzen']): return 'CPU'
        if any(x in lower for x in ['ram', 'ddr4', 'ddr5', 'memory']): return 'RAM'
        if any(x in lower for x in ['motherboard', 'mobo', 'z790', 'b650', 'x670', 'z690', 'b550']): return 'Motherboard'
        if any(x in lower for x in ['ssd', 'nvme', 'hdd', 'storage', 'samsung 9', 'wd black']): return 'Storage'
        if any(x in lower for x in ['psu', 'power supply', 'corsair rm']): return 'Power Supply'
        if any(x in lower for x in ['cooler', 'aio', 'heatsink', 'noctua']): return 'Cooling'
        
        if not GROQ_API_KEY:
            return 'Not compatible (N/A)'
            
        system_prompt = (
            "Classify the following query into ONE of these strict categories: "
            "GPU, CPU, RAM, Motherboard, Storage, Power Supply, Case, Cooling. "
            "If it is NOT a PC computer component (e.g. phones, consoles, cars, laptops, random items), return EXACTLY: Not compatible (N/A)"
        )
        try:
            res = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Query: {text}"}
                    ],
                    "temperature": 0
                },
                timeout=5
            )
            if res.ok:
                category = res.json()["choices"][0]["message"]["content"].strip()
                valid_categories = ['GPU', 'CPU', 'RAM', 'Motherboard', 'Storage', 'Power Supply', 'Case', 'Cooling']
                if category in valid_categories:
                    return category
                for vc in valid_categories:
                    if vc.lower() in category.lower():
                        return vc
        except Exception:
            pass
            
        return 'Not compatible (N/A)'

    def normalize_model(self, text: str, fallback: str) -> str:
        lower = text.lower()
        if '4070 super' in lower: return 'RTX 4070 Super'
        if '7800x3d' in lower: return 'Ryzen 7 7800X3D'
        return fallback or text

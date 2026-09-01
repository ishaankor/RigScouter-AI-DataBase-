import os
import re
import json
import asyncio
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, Query, BackgroundTasks, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

from supabase_client import supabase
from agent import TavilyHardwareAgent

load_dotenv()

app = FastAPI(title="RigScouter-AI Backend Proxy & Autonomous Tavily Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = TavilyHardwareAgent()

# ─── SSE Client Registry ──────────────────────────────────────────────────────
sse_clients = set()
sse_queue = asyncio.Queue()

async def sse_publisher():
    while True:
        event = await sse_queue.get()
        # sse-starlette uses a generator per client. We broadcast by putting into a queue
        # For simplicity, we'll keep a list of queues (one per client)
        for client_queue in list(sse_clients):
            await client_queue.put(event)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(sse_publisher())
    asyncio.create_task(scheduler_loop())

def broadcast_sse(event_name: str, data: dict):
    payload = {
        "event": event_name,
        "data": json.dumps(data)
    }
    # Put into main publisher queue
    try:
        asyncio.get_event_loop().create_task(sse_queue.put(payload))
    except RuntimeError:
        pass

def agent_sse_emitter(event_name: str, data: dict):
    broadcast_sse(event_name, data)

# ─── Dynamic Parts Queue: fetched from live watchlist & component database ───
DEFAULT_PARTS_CATALOG = [
    'RTX 4070 Super',
    'Ryzen 7 7800X3D',
    'RTX 4060',
    'RX 7800 XT',
    'Intel i5-14600K',
    'Ryzen 5 7600X',
    'Corsair Vengeance DDR5 32GB',
    'G.Skill Trident Z5 DDR5',
    'Samsung 990 Pro 2TB',
    'WD Black SN850X 1TB',
]

async def get_dynamic_parts_queue() -> list[dict]:
    """Dynamically fetches active user-monitored watchlist items and catalog components with exact product URLs."""
    items: list[dict] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()

    try:
        # 1. Fetch all catalog hardware components to build full URL index
        hw_res = await asyncio.to_thread(
            supabase.table('hardware_components')
            .select('id, name, model, category, brand, current_price, msrp, retailer, product_url, lowest_price_90d, updated_at')
            .order('deal_score', desc=True)
            .limit(500)
            .execute
        )
        hw_data = hw_res.data or []

        retailer_priority = {'Amazon': 1, 'Micro Center': 2, 'Newegg': 3, 'B&H': 4, 'Best Buy': 5, 'eBay': 6}

        PRIMARY_BRANDS = [
            'asus', 'gigabyte', 'msi', 'zotac', 'pny', 'evga', 'sapphire', 'powercolor', 'xfx', 
            'asrock', 'inno3d', 'gainward', 'palit', 'galax', 'kfa2', 'samsung', 'western digital', 
            'wd', 'seagate', 'crucial', 'sk hynix', 'sabrent', 'corsair', 'g.skill', 'gskill', 
            'kingston', 'teamgroup', 'patriot', 'adata', 'noctua', 'be quiet', 'lian li', 'nzxt', 
            'fractal', 'thermalright', 'deepcool', 'arctic', 'seasonic', 'super flower', 
            'thermaltake', 'silverstone', 'cooler master', 'montech', 'phanteks', 'antec', 'logitech', 
            'razer', 'steelseries', 'wooting', 'keychron', 'hyperx', 'shure', 'elgato', 'rode', 
            'audio-technica', 'ducky', 'epomaker', 'glorious'
        ]

        def find_best_hw_match(w_row: dict) -> dict | None:
            c_id = (w_row.get('component_id') or '').lower()
            c_name = (w_row.get('component_name') or '').lower()

            matches = []
            # 1. Exact or prefix/suffix component ID match
            for h in hw_data:
                h_id = h.get('id', '').lower()
                if h_id and c_id and (h_id == c_id or h_id.startswith(c_id) or c_id.startswith(h_id)):
                    matches.append(h)

            if not matches:
                # Brand extraction and conflict check
                c_brands = [b for b in PRIMARY_BRANDS if re.search(r'\b' + re.escape(b) + r'\b', c_name)]
                
                # Extract key alphanumeric model tokens (e.g. 5080, 4080, 7800x3d, 990, sn850x)
                clean_name = re.sub(r'[^a-z0-9\s]', ' ', c_name)
                clean_no_units = re.sub(r'\b\d+\s*(?:gb|tb|mb|mhz|ghz|w|bit)\b', '', clean_name)
                q_tokens = [t for t in clean_no_units.split() if len(t) > 2 and t not in [
                    'the', 'and', 'for', 'with', 'edition', 'gaming', 'series', 'black', 'white', 
                    'super', 'dual', 'triple', 'graphics', 'card', 'desktop', 'processor', 'solid', 
                    'state', 'drive', 'internal', 'nvme', 'power', 'supply', 'memory'
                ]]
                model_tokens = [t for t in q_tokens if re.search(r'\d', t) or len(t) >= 4]

                for h in hw_data:
                    h_text = f"{h.get('name', '')} {h.get('model', '')} {h.get('id', '')} {h.get('brand', '')}".lower()
                    
                    # Brand conflict check: If watchlist specifies brand A, do not match candidate with brand B
                    if c_brands:
                        has_brand = any(re.search(r'\b' + re.escape(b) + r'\b', h_text) for b in c_brands)
                        h_conflicts = [b for b in PRIMARY_BRANDS if b not in c_brands and re.search(r'\b' + re.escape(b) + r'\b', h_text)]
                        if not has_brand and h_conflicts:
                            continue

                    # Model token check: ALL critical digit tokens from query must exist in candidate
                    if model_tokens:
                        digit_tokens = [t for t in model_tokens if re.search(r'\d', t)]
                        if digit_tokens and not all(d in h_text for d in digit_tokens):
                            continue
                        
                        score = sum(1 for t in model_tokens if t in h_text)
                        if score >= max(1, len(model_tokens) - 1):
                            matches.append(h)

            if not matches:
                return None

            # Sort by presence of valid product URL, retailer reliability, and price
            matches.sort(key=lambda m: (
                0 if (m.get('product_url') and m.get('product_url').startswith('http') and m.get('product_url') != '#') else 1,
                retailer_priority.get(m.get('retailer'), 99),
                float(m.get('current_price') or 999999)
            ))
            return matches[0]

        # 2. Fetch user watchlist items (highest priority)
        wl_res = await asyncio.to_thread(
            supabase.table('watchlist_items')
            .select('id, user_id, component_name, component_id, category, target_price, all_time_low, added_at')
            .limit(100)
            .execute
        )
        for r in (wl_res.data or []):
            name = r.get('component_name')
            if not name:
                continue

            matched_hw = find_best_hw_match(r)
            url = matched_hw.get('product_url') if matched_hw else None
            retailer = matched_hw.get('retailer') if matched_hw else 'Amazon'
            current_price = float(matched_hw.get('current_price') or r.get('all_time_low') or 0.0) if matched_hw else float(r.get('all_time_low') or 0.0)
            updated_at_val = matched_hw.get('updated_at') if matched_hw else r.get('added_at')

            clean_url = url.strip() if (url and url.startswith('http') and url != '#') else None
            key = clean_url or name.lower()
            if key not in seen_urls and key not in seen_names:
                if clean_url:
                    seen_urls.add(clean_url)
                seen_names.add(name.lower())
                items.append({
                    "id": r.get('id'),
                    "userId": r.get('user_id'),
                    "name": name,
                    "category": r.get('category') or 'GPU',
                    "targetPrice": float(r['target_price']) if r.get('target_price') else 0.0,
                    "currentPrice": current_price,
                    "retailer": retailer,
                    "url": clean_url,
                    "notify": True,
                    "isWatchlist": True,
                    "updatedAt": updated_at_val
                })

        # 3. Append remaining catalog components
        for r in hw_data:
            name = r.get('model') or r.get('name')
            url = r.get('product_url')
            if not name:
                continue

            clean_url = url.strip() if (url and url.startswith('http') and url != '#') else None
            key = clean_url or name.lower()
            if key not in seen_urls and key not in seen_names:
                if clean_url:
                    seen_urls.add(clean_url)
                seen_names.add(name.lower())
                items.append({
                    "id": r.get('id'),
                    "name": name,
                    "category": r.get('category') or 'GPU',
                    "currentPrice": float(r.get('current_price') or 0.0),
                    "retailer": r.get('retailer') or 'Amazon',
                    "url": clean_url,
                    "notify": False,
                    "isWatchlist": False,
                    "updatedAt": r.get('updated_at')
                })

        if items:
            return items
    except Exception as e:
        print(f"[Dynamic Parts Queue Notice]: {e}")

    # Fallback to DEFAULT_PARTS_CATALOG if DB is empty
    return [{"id": f"default-{i}", "name": name, "category": "GPU", "currentPrice": 0.0, "retailer": "Amazon", "url": None, "notify": False, "isWatchlist": False, "updatedAt": None} for i, name in enumerate(DEFAULT_PARTS_CATALOG)]

# ─── Scheduler State (Daily at 12:00 AM UTC) ──────────────────────────────────
scheduler_state = {
    "schedulerRunning": False,
    "lastSchedulerRun": None,
    "nextScheduledRun": None,
    "scheduleMode": "Daily at 12:00 AM UTC (00:00 UTC)",
    "lastBatchSummary": None,
}

def get_seconds_until_next_midnight_utc() -> float:
    now = datetime.now(timezone.utc)
    # Next midnight (00:00:00 UTC tomorrow)
    tomorrow_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow_midnight - now).total_seconds())

async def run_full_daily_batch():
    if scheduler_state["schedulerRunning"]:
        print("[Daily Scheduler] Previous refresh batch is still running — skipping invocation")
        return {"status": "in_progress", "message": "Daily scheduler is currently running"}

    scheduler_state["schedulerRunning"] = True
    start_time = datetime.now(timezone.utc)
    scheduler_state["lastSchedulerRun"] = start_time.isoformat()
    # Disable Firecrawl in batch to preserve credits
    agent._disable_firecrawl_in_batch = True

    try:
        queue = await get_dynamic_parts_queue()
        total_items = len(queue)
        print(f"\n[Daily Scheduler] 🌟 [12:00 AM UTC Daily Refresh Started] Updating {total_items} tracked items (Watchlists prioritized, 0-credit Direct HTTP for catalog)...")
        
        broadcast_sse('daily_refresh_start', {
            "totalItems": total_items,
            "timestamp": scheduler_state["lastSchedulerRun"]
        })

        success_count = 0
        for i, item in enumerate(queue):
            item_name = item.get("name") if isinstance(item, dict) else str(item)
            item_url = item.get("url") if isinstance(item, dict) else None
            retailer = item.get("retailer") if isinstance(item, dict) else "Online Retailer"
            is_watchlist = bool(item.get("isWatchlist") if isinstance(item, dict) else False)
            updated_at_str = item.get("updatedAt") if isinstance(item, dict) else None
            pos = i + 1

            # Freshness TTL Check: Skip items updated < 18 hours ago that already have a valid price
            if updated_at_str and isinstance(item, dict) and item.get("currentPrice", 0) > 0:
                try:
                    clean_iso = updated_at_str.replace("Z", "+00:00")
                    item_dt = datetime.fromisoformat(clean_iso)
                    if item_dt.tzinfo is None:
                        item_dt = item_dt.replace(tzinfo=timezone.utc)
                    hours_since = (datetime.now(timezone.utc) - item_dt).total_seconds() / 3600.0
                    if hours_since < 18.0:
                        print(f"[Daily Scheduler TTL] ⏭️ ({pos}/{total_items}) Skipping \"{item_name}\": already fresh ({hours_since:.1f}h ago)")
                        success_count += 1
                        continue
                except Exception:
                    pass

            print(f"\n[Daily Scheduler] ⏱ ({pos}/{total_items}) Auto-updating: \"{item_name}\" ({retailer}) [Watchlist: {is_watchlist}]")
            broadcast_sse('scheduler_tick', {
                "query": item_name,
                "url": item_url,
                "retailer": retailer,
                "itemPosition": pos,
                "totalQueue": total_items,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })

            direct_success = False
            # Phase 1: Direct Product URL scraping (instant, 100% accurate, 0 API search credits)
            if item_url and item_url.startswith('http'):
                print(f"[Daily Scheduler Direct URL] 🎯 Checking exact URL ({retailer}): {item_url}")
                try:
                    offer = await agent.refresh_direct_item(item, agent_sse_emitter)
                    if offer and offer.get('price', 0) > 0:
                        print(f"✅ [Daily Scheduler Direct URL Success] Refreshed \"{item_name}\" -> ${offer['price']:.2f} at {offer.get('retailer', retailer)}")
                        direct_success = True
                        success_count += 1
                except Exception as direct_err:
                    print(f"⚠️ [Daily Scheduler Direct URL Notice] Failed direct refresh for \"{item_name}\": {direct_err}")

            # Phase 2: Web Search Fallback ONLY for active user watchlists
            if not direct_success:
                if is_watchlist:
                    try:
                        print(f"[Daily Scheduler Watchlist Search] 🔍 Unified search for user-tracked item: \"{item_name}\"")
                        res = await agent.run(item_name, agent_sse_emitter, user_id=item.get('userId') if isinstance(item, dict) else None)
                        if res and not res.get('is_error'):
                            success_count += 1
                    except Exception as search_err:
                        print(f"⚠️ [Daily Scheduler Search Notice] Error for \"{item_name}\": {search_err}")
                else:
                    print(f"ℹ️ [Daily Scheduler Catalog Item] \"{item_name}\" (no direct URL/scrape unavailable). Skipping multi-retailer web search to preserve API credits.")

            # Gentle pause between items to prevent rate-limits
            if i < total_items - 1:
                await asyncio.sleep(1.5)

        end_time = datetime.now(timezone.utc)
        duration_secs = round((end_time - start_time).total_seconds(), 1)
        summary_msg = f"Refreshed {success_count}/{total_items} items in {duration_secs}s"
        scheduler_state["lastBatchSummary"] = summary_msg
        print(f"\n[Daily Scheduler] 🎉 [12:00 AM UTC Daily Refresh Completed] {summary_msg}.")

        broadcast_sse('daily_refresh_complete', {
            "totalItems": total_items,
            "successCount": success_count,
            "durationSeconds": duration_secs,
            "timestamp": end_time.isoformat()
        })
        return {
            "status": "completed",
            "totalItems": total_items,
            "successCount": success_count,
            "durationSeconds": duration_secs,
            "timestamp": end_time.isoformat()
        }
    except Exception as e:
        print(f"[Daily Scheduler Error]: {e}")
        broadcast_sse('scheduler_error', {
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        return {"status": "error", "error": str(e)}
    finally:
        agent._disable_firecrawl_in_batch = False
        scheduler_state["schedulerRunning"] = False

async def scheduler_loop():
    print("[Daily Scheduler] Initialized daily price updater — runs across ALL tracked items at 12:00 AM UTC (00:00 UTC) every day.")
    while True:
        secs = get_seconds_until_next_midnight_utc()
        next_run_dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
        scheduler_state["nextScheduledRun"] = next_run_dt.isoformat()
        hours = secs / 3600
        print(f"[Daily Scheduler] ⏳ Next 12:00 AM UTC catalog refresh in {hours:.2f} hours ({int(secs)}s) at {scheduler_state['nextScheduledRun']}")
        await asyncio.sleep(secs)
        await run_full_daily_batch()

# ─── Pure API Endpoints ──────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "RigScouter-AI Backend Proxy & Autonomous Tavily Agent",
        "databaseConnected": True,
        "schedulerActive": True,
        "schedule": "Daily at 12:00 AM UTC (00:00 UTC)",
        "nextScheduledRun": scheduler_state.get("nextScheduledRun"),
        "lastSchedulerRun": scheduler_state.get("lastSchedulerRun"),
        "lastBatchSummary": scheduler_state.get("lastBatchSummary"),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "RigScouter-AI-DataBase",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/status")
async def api_status():
    queue = await get_dynamic_parts_queue()
    return {
        "schedulerRunning": scheduler_state["schedulerRunning"],
        "lastSchedulerRun": scheduler_state["lastSchedulerRun"],
        "nextScheduledRun": scheduler_state.get("nextScheduledRun"),
        "scheduleMode": scheduler_state.get("scheduleMode"),
        "lastBatchSummary": scheduler_state.get("lastBatchSummary"),
        "partsQueue": [item.get('name') if isinstance(item, dict) else str(item) for item in queue],
        "totalParts": len(queue),
        "connectedSseClients": len(sse_clients),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/cron/trigger-daily-update")
@app.post("/api/cron/trigger-daily-update")
async def trigger_daily_update(background_tasks: BackgroundTasks):
    """Refreshes live prices across ALL tracked hardware components and records timestamped PriceHistory snapshots."""
    queue = await get_dynamic_parts_queue()
    background_tasks.add_task(run_full_daily_batch)
    return {
        "status": "started",
        "message": f"Full daily price update initiated for all {len(queue)} component(s) in background.",
        "components": [item.get('name') if isinstance(item, dict) else str(item) for item in queue],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/stream")
async def stream(request: Request):
    client_queue = asyncio.Queue()
    sse_clients.add(client_queue)
    
    async def event_generator():
        try:
            queue = await get_dynamic_parts_queue()
            # Initial connection event
            yield {
                "event": "connected",
                "data": json.dumps({
                    "message": "Connected to RigScouter-AI live price feed",
                    "schedulerRunning": scheduler_state["schedulerRunning"],
                    "lastSchedulerRun": scheduler_state["lastSchedulerRun"],
                    "nextScheduledRun": scheduler_state.get("nextScheduledRun"),
                    "scheduleMode": scheduler_state.get("scheduleMode"),
                    "totalTracked": len(queue),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(client_queue.get(), timeout=30.0)
                    yield event
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()})
                    }
        finally:
            sse_clients.remove(client_queue)

    return EventSourceResponse(event_generator())

@app.get("/api/components")
async def get_components(category: str = None, q: str = None):
    try:
        query = supabase.table('hardware_components').select('*')
        if category:
            query = query.eq('category', category)
        if q:
            # Supabase-py doesn't support complex .or natively as easily as JS without text, but we can do it via eq or ilike if supported.
            # Using raw postgrest filtering
            query = query.or_(f"name.ilike.%{q}%,model.ilike.%{q}%,brand.ilike.%{q}%")
            
        query_obj = query.order('updated_at', desc=True).limit(50)
        res = await asyncio.to_thread(query_obj.execute)
        
        formatted = []
        for item in res.data:
            formatted.append({
                "id": item.get('id'),
                "name": item.get('name'),
                "category": item.get('category'),
                "brand": item.get('brand'),
                "model": item.get('model'),
                "specs": json.loads(item.get('specs')) if isinstance(item.get('specs'), str) else (item.get('specs') or {}),
                "msrp": item.get('msrp'),
                "currentPrice": item.get('current_price'),
                "lowestPrice90d": item.get('lowest_price_90d'),
                "retailer": item.get('retailer'),
                "productUrl": item.get('product_url'),
                "imageUrl": item.get('image_url'),
                "rating": item.get('rating'),
                "dealScore": item.get('deal_score'),
                "updatedAt": item.get('updated_at')
            })
        return {"source": "supabase_database", "components": formatted}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/watchlist")
async def get_watchlist():
    try:
        query = supabase.table('watchlist_items').select('*').order('id', desc=True)
        res = await asyncio.to_thread(query.execute)
        formatted = []
        for item in res.data:
            formatted.append({
                "id": item.get('id'),
                "userId": item.get('user_id'),
                "componentName": item.get('component_name'),
                "category": item.get('category'),
                "targetPrice": item.get('target_price'),
                "currentPrice": item.get('current_price'),
                "previousPrice24h": item.get('previous_price_24h'),
                "previousPrice7d": item.get('previous_price_7d'),
                "previousPrice30d": item.get('previous_price_30d'),
                "allTimeLow": item.get('all_time_low'),
                "retailer": item.get('retailer'),
                "productUrl": item.get('product_url'),
                "imageUrl": item.get('image_url'),
                "inStock": item.get('in_stock'),
                "notifyOnFlashDrop": item.get('notify_on_flash_drop'),
                "addedAt": item.get('added_at')
            })
        return {"source": "supabase_database", "items": formatted}
    except Exception as e:
        return {"error": str(e)}

async def _run_scrape_in_background(
    clean_query: str,
    user_id: str = None,
    pending_id: str = None,
):
    """Fire-and-forget: run the agent and push all events via SSE."""
    try:
        print(f"[BG Scrape] Starting: \"{clean_query}\"")
        await agent.run(clean_query, agent_sse_emitter, user_id, pending_id)
    except Exception as e:
        print(f"[BG Scrape Error] \"{clean_query}\": {e}")
        broadcast_sse("agent_error", {
            "query": clean_query,
            "error": str(e),
            "pending_id": pending_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


async def handle_database_proxy_scrape(target_query: str, user_id: str = None, pending_id: str = None):
    try:
        clean_query = target_query.strip()

        # Step A: Check DB cache — return instantly if we have a fresh result.
        db_query = supabase.table('hardware_components').select('*') \
            .or_(f"name.ilike.%{clean_query}%,model.ilike.%{clean_query}%,product_url.ilike.%{clean_query}%") \
            .order('updated_at', desc=True).limit(1)
        res = await asyncio.to_thread(db_query.execute)

        if res.data and len(res.data) > 0:
            match = res.data[0]
            print(f"[DB Match Hit] Returning \"{match['name']}\" (${match['current_price']}) from Supabase DB")
            return {
                "source": 'supabase_database_cache',
                "query": clean_query,
                "scrapedAt": match['updated_at'],
                "bestOffer": {
                    "title": match['name'],
                    "price": match['current_price'],
                    "originalPrice": match['msrp'],
                    "retailer": match['retailer'],
                    "url": match['product_url'],
                    "brand": match['brand'],
                    "inStock": True
                },
                "component": {
                    "id": match['id'],
                    "name": match['name'],
                    "category": match['category'],
                    "brand": match['brand'],
                    "model": match['model'],
                    "specs": json.loads(match['specs']) if isinstance(match['specs'], str) else (match.get('specs') or {}),
                    "msrp": match['msrp'],
                    "currentPrice": match['current_price'],
                    "lowestPrice90d": match['lowest_price_90d'],
                    "retailer": match['retailer'],
                    "productUrl": match['product_url'],
                    "imageUrl": match.get('image_url', 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80'),
                    "rating": match.get('rating', 4.8),
                    "dealScore": match.get('deal_score', 85)
                }
            }

        # Step B: DB miss — kick off a background scrape and return immediately.
        # Results arrive asynchronously via SSE (agent_start / retailer_found /
        # agent_complete events), so the HTTP connection is freed right away.
        print(f"[DB Miss] \"{clean_query}\" missing from DB. Executing Agent to scrape & add to database...")
        asyncio.create_task(
            _run_scrape_in_background(clean_query, user_id, pending_id)
        )

        return {
            "source": "agent_scraping",
            "query": clean_query,
            "message": "Scrape started. Results will arrive via SSE (agent_complete event).",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        print("[Backend Proxy Error]:", str(e))
        import traceback; traceback.print_exc()
        return {"error": str(e)}

@app.post("/api/scrape")
async def post_scrape(request: Request):
    body = await request.json()
    query = body.get('query') or body.get('prompt') or body.get('url')
    user_id = body.get('userId')
    pending_id = body.get('pendingId')
    if not query:
        return {"error": "Provide valid search query or URL in request body"}
    return await handle_database_proxy_scrape(query, user_id, pending_id)

@app.get("/api/scrape")
async def get_scrape(query: str = None, q: str = None):
    target = query or q
    if not target:
        return {"error": "Provide ?query= parameter"}
    return await handle_database_proxy_scrape(target)

@app.post("/api/agent/run")
async def post_agent_run(request: Request):
    body = await request.json()
    query = body.get('query') or body.get('prompt') or body.get('url')
    user_id = body.get('userId')
    pending_id = body.get('pendingId')
    if not query:
        return {"error": "Provide valid search query or URL in request body"}
    return await handle_database_proxy_scrape(query, user_id, pending_id)

@app.get("/api/agent/run")
async def get_agent_run(query: str = None, q: str = None):
    target = query or q
    if not target:
        return {"error": "Provide ?query= parameter"}
    return await handle_database_proxy_scrape(target)

@app.delete("/api/watchlist/{id}")
async def delete_watchlist(id: str):
    import re
    try:
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', id, re.I):
            query = supabase.table('watchlist_items').delete().eq('id', id)
            await asyncio.to_thread(query.execute)
        else:
            comp_id = id.replace('w-', 'comp-', 1) if id.startswith('w-') else id
            # Delete from watchlist_items by component_id
            wl_query = supabase.table('watchlist_items').delete().ilike('component_id', f"%{comp_id.replace('comp-', '')}%")
            await asyncio.to_thread(wl_query.execute)
            # Also clean up any hardware_components entry if it was user-specific
            hw_query = supabase.table('hardware_components').delete().eq('id', comp_id)
            await asyncio.to_thread(hw_query.execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/api/watchlist")
@app.post("/api/watchlist/update-target")
async def update_watchlist_target(request: Request):
    try:
        body = await request.json()
        item_id = body.get("id")
        ids = body.get("ids", [])
        user_id = body.get("userId")
        component_name = body.get("componentName")
        target_price = body.get("targetPrice")
        notify = body.get("notifyOnFlashDrop")

        payload = {}
        if target_price is not None:
            payload["target_price"] = float(target_price)
        if notify is not None:
            payload["notify_on_flash_drop"] = bool(notify)

        if not payload:
            return {"error": "No updates provided"}

        uuid_regex = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
        target_ids = [item_id] + (ids if isinstance(ids, list) else [])
        valid_uuids = []
        for tid in target_ids:
            if not tid:
                continue
            clean_id = re.sub(r'^(w-|hw-|comp-)', '', str(tid))
            if uuid_regex.match(clean_id):
                valid_uuids.append(clean_id)

        matched_rows = []
        if valid_uuids:
            res = await asyncio.to_thread(supabase.table('watchlist_items').select('*').in_('id', valid_uuids).execute)
            if res.data:
                matched_rows.extend(res.data)

        if user_id and component_name and not matched_rows:
            clean_name = re.sub(r'[^a-zA-Z0-9\s]', ' ', component_name).strip()[:30]
            if clean_name:
                res2 = await asyncio.to_thread(supabase.table('watchlist_items').select('*').eq('user_id', user_id).ilike('component_name', f"%{clean_name}%").execute)
                if res2.data:
                    matched_rows.extend(res2.data)

        # Atomic replace
        for row in matched_rows:
            await asyncio.to_thread(supabase.table('watchlist_items').delete().eq('id', row['id']).execute)
            updated_row = {
                "user_id": row.get("user_id"),
                "component_id": row.get("component_id"),
                "component_name": row.get("component_name"),
                "category": row.get("category"),
                "target_price": payload.get("target_price", row.get("target_price")),
                "previous_price_24h": row.get("previous_price_24h"),
                "previous_price_7d": row.get("previous_price_7d"),
                "previous_price_30d": row.get("previous_price_30d"),
                "all_time_low": row.get("all_time_low"),
                "created_at": row.get("created_at"),
                "added_at": row.get("added_at"),
            }
            await asyncio.to_thread(supabase.table('watchlist_items').insert(updated_row).execute)

        return {"success": True, "updates": payload, "updatedCount": len(matched_rows)}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/components/{id}")
async def delete_component(id: str):
    try:
        query = supabase.table('hardware_components').delete().eq('id', id)
        res = await asyncio.to_thread(query.execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/scheduler/run-now")
async def run_scheduler_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_full_daily_batch)
    return {"message": "Full daily price refresh triggered across all tracked components", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.get("/api/keep-alive")
@app.get("/health/supabase")
@app.get("/keep-alive")
async def keep_alive_endpoint():
    """Live database query endpoint to keep Supabase and backend active."""
    import time
    start_time = time.perf_counter()
    try:
        query = supabase.table('hardware_components').select('id, name, updated_at').limit(3)
        res = await asyncio.to_thread(query.execute)
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "status": "online",
            "database": "connected",
            "latencyMs": latency_ms,
            "itemsRetrieved": len(res.data or []),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "Supabase keep-alive ping successful"
        }
    except Exception as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "status": "warning",
            "database": "error",
            "error": str(e),
            "latencyMs": latency_ms,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


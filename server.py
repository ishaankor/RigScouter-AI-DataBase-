import os
import json
import asyncio
from datetime import datetime, timezone
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
    # asyncio.create_task(scheduler_loop())

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

async def get_dynamic_parts_queue() -> list[str]:
    """Dynamically fetches active user-monitored watchlist items and trending components."""
    try:
        wl_res = await asyncio.to_thread(
            supabase.table('watchlist_items').select('component_name').limit(50).execute
        )
        parts = [r['component_name'] for r in (wl_res.data or []) if r.get('component_name')]

        hw_res = await asyncio.to_thread(
            supabase.table('hardware_components').select('model, name').order('deal_score', desc=True).limit(25).execute
        )
        for r in (hw_res.data or []):
            item_name = r.get('model') or r.get('name')
            if item_name and item_name not in parts:
                parts.append(item_name)

        if parts:
            return parts
    except Exception as e:
        print(f"[Dynamic Parts Queue Notice]: {e}")

    return DEFAULT_PARTS_CATALOG

# ─── Scheduler State ──────────────────────────────────────────────────────────
scheduler_state = {
    "schedulerRunning": False,
    "lastSchedulerRun": None,
    "schedulerQueueIndex": 0,
}
SCHEDULER_INTERVAL_SECONDS = 15 * 60

async def run_scheduler_tick():
    if scheduler_state["schedulerRunning"]:
        print("[Scheduler] Previous run still in progress — skipping tick")
        return

    scheduler_state["schedulerRunning"] = True
    queue = await get_dynamic_parts_queue()
    part = queue[scheduler_state["schedulerQueueIndex"] % len(queue)]
    scheduler_state["schedulerQueueIndex"] += 1
    scheduler_state["lastSchedulerRun"] = datetime.now(timezone.utc).isoformat()

    print(f"\n[Scheduler] ⏱ Auto-updating daily prices for: \"{part}\" ({scheduler_state['schedulerQueueIndex']}/{len(queue)})")
    broadcast_sse('scheduler_tick', {
        "query": part,
        "queueIndex": scheduler_state["schedulerQueueIndex"],
        "timestamp": scheduler_state["lastSchedulerRun"]
    })

    try:
        await agent.run(part, agent_sse_emitter)
    except Exception as e:
        print(f"[Scheduler Error] \"{part}\":", str(e))
        broadcast_sse('scheduler_error', {
            "query": part,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
    finally:
        scheduler_state["schedulerRunning"] = False

async def scheduler_loop():
    print(f"[Scheduler] Starting dynamic price updater — scraping every {SCHEDULER_INTERVAL_SECONDS / 60} minutes")
    await asyncio.sleep(5)
    await run_scheduler_tick()
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        await run_scheduler_tick()

# ─── Pure API Endpoints ──────────────────────────────────────────

@app.get("/")
async def root():
    queue = await get_dynamic_parts_queue()
    return {
        "status": "ok",
        "service": "RigScouter-AI Backend Proxy & Autonomous Tavily Agent",
        "databaseConnected": True,
        "schedulerActive": True,
        "intervalMinutes": SCHEDULER_INTERVAL_SECONDS / 60,
        "nextScheduledPart": queue[scheduler_state["schedulerQueueIndex"] % len(queue)],
        "trackedCount": len(queue),
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
        "schedulerQueueIndex": scheduler_state["schedulerQueueIndex"],
        "nextPart": queue[scheduler_state["schedulerQueueIndex"] % len(queue)],
        "partsQueue": queue,
        "totalParts": len(queue),
        "intervalMinutes": SCHEDULER_INTERVAL_SECONDS / 60,
        "connectedSseClients": len(sse_clients),
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/stream")
async def stream(request: Request):
    client_queue = asyncio.Queue()
    sse_clients.add(client_queue)
    
    async def event_generator():
        try:
            queue = await get_dynamic_parts_queue()
            next_part = queue[scheduler_state["schedulerQueueIndex"] % len(queue)] if queue else "N/A"
            # Initial connection event
            yield {
                "event": "connected",
                "data": json.dumps({
                    "message": "Connected to RigScouter-AI live price feed",
                    "schedulerRunning": scheduler_state["schedulerRunning"],
                    "lastSchedulerRun": scheduler_state["lastSchedulerRun"],
                    "nextPart": next_part,
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
    background_tasks.add_task(run_scheduler_tick)
    return {"message": "Scheduler tick triggered", "timestamp": datetime.now(timezone.utc).isoformat()}

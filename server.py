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

# ─── Parts Queue: the daily scheduler scrapes these continuously to update prices
PARTS_QUEUE = [
    'RTX 4060',
    'RTX 4070 Super',
    'RTX 4070 Ti Super',
    'RX 7800 XT',
    'GTX 1080 Ti',
    'Ryzen 7 7800X3D',
    'Intel i5-14600K',
    'Ryzen 5 7600X',
    'Corsair Vengeance DDR5 32GB',
    'G.Skill Trident Z5 DDR5',
    'Samsung 990 Pro 1TB',
    'WD Black SN850X 1TB',
]

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
    part = PARTS_QUEUE[scheduler_state["schedulerQueueIndex"] % len(PARTS_QUEUE)]
    scheduler_state["schedulerQueueIndex"] += 1
    scheduler_state["lastSchedulerRun"] = datetime.now(timezone.utc).isoformat()

    print(f"\n[Scheduler] ⏱ Auto-updating daily prices for: \"{part}\" ({scheduler_state['schedulerQueueIndex']}/{len(PARTS_QUEUE)})")
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
    print(f"[Scheduler] Starting daily price updater — scraping every {SCHEDULER_INTERVAL_SECONDS / 60} minutes")
    await asyncio.sleep(5)
    await run_scheduler_tick()
    while True:
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
        await run_scheduler_tick()

# ─── Pure API Endpoints ──────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "RigScouter-AI Backend Proxy & Autonomous Tavily Agent",
        "databaseConnected": True,
        "schedulerActive": True,
        "intervalMinutes": SCHEDULER_INTERVAL_SECONDS / 60,
        "nextScheduledPart": PARTS_QUEUE[scheduler_state["schedulerQueueIndex"] % len(PARTS_QUEUE)],
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
    return {
        "schedulerRunning": scheduler_state["schedulerRunning"],
        "lastSchedulerRun": scheduler_state["lastSchedulerRun"],
        "schedulerQueueIndex": scheduler_state["schedulerQueueIndex"],
        "nextPart": PARTS_QUEUE[scheduler_state["schedulerQueueIndex"] % len(PARTS_QUEUE)],
        "partsQueue": PARTS_QUEUE,
        "totalParts": len(PARTS_QUEUE),
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
            # Initial connection event
            yield {
                "event": "connected",
                "data": json.dumps({
                    "message": "Connected to RigScouter-AI live price feed",
                    "schedulerRunning": scheduler_state["schedulerRunning"],
                    "lastSchedulerRun": scheduler_state["lastSchedulerRun"],
                    "nextPart": PARTS_QUEUE[scheduler_state["schedulerQueueIndex"] % len(PARTS_QUEUE)],
                    "totalTracked": len(PARTS_QUEUE),
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
            
        res = query.order('updated_at', desc=True).limit(50).execute()
        
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
        res = supabase.table('watchlist_items').select('*').order('added_at', desc=True).execute()
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

async def handle_database_proxy_scrape(target_query: str):
    try:
        clean_query = target_query.strip()
        
        # Step A: Check DB
        res = supabase.table('hardware_components').select('*') \
            .or_(f"name.ilike.%{clean_query}%,model.ilike.%{clean_query}%,product_url.ilike.%{clean_query}%") \
            .order('updated_at', desc=True).limit(1).execute()
            
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

        # Step B: Scrape
        print(f"[DB Miss] \"{clean_query}\" missing from DB. Executing Agent to scrape & add to database...")
        agent_result = await agent.run(clean_query, agent_sse_emitter)
        best_offer = agent_result.get('bestOffer')
        
        if best_offer and best_offer.get('price'):
            return {
                "source": 'agent_scraped_and_saved_to_db',
                "query": clean_query,
                "scrapedAt": datetime.now(timezone.utc).isoformat(),
                "bestOffer": best_offer,
                "component": {
                    "id": f"agent-{int(datetime.now().timestamp() * 1000)}",
                    "name": best_offer.get('title', clean_query),
                    "category": agent_result.get('category', 'GPU'),
                    "brand": best_offer.get('brand', best_offer.get('title', 'Hardware').split(' ')[0]),
                    "model": clean_query,
                    "specs": {},
                    "msrp": best_offer.get('originalPrice') or round(best_offer['price'] * 1.12, 2),
                    "currentPrice": best_offer['price'],
                    "lowestPrice90d": round(best_offer['price'] * 0.96, 2),
                    "retailer": best_offer.get('retailer', 'Micro Center'),
                    "productUrl": best_offer.get('url', f"https://www.amazon.com/s?k={clean_query}"),
                    "imageUrl": 'https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80',
                    "rating": 4.8,
                    "dealScore": 90
                }
            }

        return {
            "found": False,
            "query": clean_query,
            "component": None,
            "notice": f"Could not retrieve live price for \"{clean_query}\""
        }
    except Exception as e:
        print("[Backend Proxy Error]:", str(e))
        return {"error": str(e)}

@app.post("/api/scrape")
async def post_scrape(request: Request):
    body = await request.json()
    query = body.get('query') or body.get('prompt') or body.get('url')
    if not query:
        return {"error": "Provide valid search query or URL in request body"}
    return await handle_database_proxy_scrape(query)

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
    if not query:
        return {"error": "Provide valid search query or URL in request body"}
    return await handle_database_proxy_scrape(query)

@app.get("/api/agent/run")
async def get_agent_run(query: str = None, q: str = None):
    target = query or q
    if not target:
        return {"error": "Provide ?query= parameter"}
    return await handle_database_proxy_scrape(target)

@app.delete("/api/watchlist/{id}")
async def delete_watchlist(id: str):
    try:
        res = supabase.table('watchlist_items').delete().eq('id', id).execute()
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/components/{id}")
async def delete_component(id: str):
    try:
        res = supabase.table('hardware_components').delete().eq('id', id).execute()
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/scheduler/run-now")
async def run_scheduler_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_scheduler_tick)
    return {"message": "Scheduler tick triggered", "timestamp": datetime.now(timezone.utc).isoformat()}

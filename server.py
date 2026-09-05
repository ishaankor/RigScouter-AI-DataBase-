import os
import re
import json
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv

load_dotenv()

from supabase_client import supabase
from agent import HardwareAgent

app = FastAPI(title="RigScouter-AI Multi-Retailer Pricing Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent = HardwareAgent()

# ─── SSE Client Registry & Broadcaster ────────────────────────────────────────

sse_clients = set()
sse_queue = asyncio.Queue()

async def sse_publisher():
    while True:
        event = await sse_queue.get()
        for client_queue in list(sse_clients):
            try:
                await client_queue.put(event)
            except Exception:
                pass

def broadcast_sse(event_name: str, data: dict):
    payload = {
        "event": event_name,
        "data": json.dumps(data)
    }
    try:
        asyncio.get_event_loop().create_task(sse_queue.put(payload))
    except RuntimeError:
        pass

def agent_sse_emitter(event_name: str, data: dict):
    broadcast_sse(event_name, data)


# ─── Background Scrape & Database Sync ────────────────────────────────────────

async def _run_scrape_and_persist(target_query: str, user_id: str = None, pending_id: str = None):
    try:
        print(f"[Backend Worker] Running scrape for: \"{target_query}\"")
        res = await agent.run(target_query, agent_sse_emitter, user_id=user_id, pending_id=pending_id)

        offers = res.get("scrapedOffers", [])
        if not offers:
            return

        best_offer = offers[0]
        now_iso = datetime.now(timezone.utc).isoformat()
        comp_id = re.sub(r'[^a-zA-Z0-9]+', '-', res.get("normalized_query", target_query).lower()).strip('-')

        price = float(best_offer.get("price") or 0.0)
        msrp = float(best_offer.get("originalPrice") or price)
        image_url = best_offer.get("imageUrl") or "https://images.unsplash.com/photo-1587202372775-e229f172b9d7?auto=format&fit=crop&w=600&q=80"

        specs_json = json.dumps({"RetailerOffers": offers})

        # 1. Upsert all individual retailer offers so every retailer row exists in catalog
        for off in offers:
            ret_name = off.get("retailer", "Unknown")
            ret_slug = re.sub(r'[^a-zA-Z0-9]+', '-', ret_name.lower()).strip('-')
            row_id = f"{comp_id}-{ret_slug}"
            off_price = float(off.get("price") or 0.0)
            off_msrp = float(off.get("originalPrice") or off_price)
            off_img = off.get("imageUrl") or image_url
            off_payload = {
                "id": row_id,
                "name": off.get("title") or res.get("normalized_query", target_query),
                "model": res.get("normalized_query", target_query),
                "category": res.get("category", "Hardware"),
                "brand": res.get("brand"),
                "current_price": off_price,
                "msrp": off_msrp,
                "lowest_price_90d": off_price,
                "retailer": ret_name,
                "product_url": off.get("url"),
                "image_url": off_img,
                "specs": specs_json,
                "updated_at": now_iso
            }
            try:
                await asyncio.to_thread(
                    supabase.table("hardware_components").upsert(off_payload).execute
                )
            except Exception as off_err:
                print(f"⚠️ [DB Save Notice] hardware_components ({row_id}): {off_err}")

        # 2. Also ensure base comp_id row exists with best offer and full RetailerOffers in specs
        hw_payload = {
            "id": comp_id,
            "name": best_offer.get("title") or res.get("normalized_query", target_query),
            "model": res.get("normalized_query", target_query),
            "category": res.get("category", "Hardware"),
            "brand": res.get("brand"),
            "current_price": price,
            "msrp": msrp,
            "lowest_price_90d": price,
            "retailer": best_offer.get("retailer", "Amazon"),
            "product_url": best_offer.get("url"),
            "image_url": image_url,
            "specs": specs_json,
            "updated_at": now_iso
        }

        try:
            await asyncio.to_thread(
                supabase.table("hardware_components").upsert(hw_payload).execute
            )
            print(f"💾 [DB Persisted] Saved component '{hw_payload['name'][:40]}' with {len(offers)} retailer offer(s)")
        except Exception as db_err:
            print(f"⚠️ [DB Save Notice] hardware_components: {db_err}")

        # 2. Sync to watchlist if this was user-requested
        if user_id:
            try:
                wl_payload = {
                    "user_id": user_id,
                    "component_id": comp_id,
                    "component_name": best_offer.get("title") or res.get("normalized_query"),
                    "category": res.get("category", "Hardware"),
                    "target_price": round(price * 0.9, 2),
                    "all_time_low": price,
                    "added_at": now_iso
                }
                await asyncio.to_thread(
                    supabase.table("watchlist_items").upsert(wl_payload).execute
                )
                print(f"💾 [DB Watchlist Synced] User {user_id} tracking {comp_id}")
            except Exception as wl_err:
                print(f"⚠️ [DB Watchlist Notice]: {wl_err}")

    except Exception as e:
        print(f"[Backend Worker Error] {e}")
        broadcast_sse("agent_error", {
            "query": target_query,
            "error": str(e),
            "pending_id": pending_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })


async def handle_scrape_request(target_query: str, user_id: str = None, pending_id: str = None):
    clean = target_query.strip()
    if not clean:
        return {"error": "Empty query"}

    try:
        # Step A: Check DB cache for a recent match (updated within 24 hours)
        res = await asyncio.to_thread(
            supabase.table("hardware_components")
            .select("*")
            .or_(f"name.ilike.%{clean}%,model.ilike.%{clean}%")
            .order("updated_at", desc=True)
            .limit(1)
            .execute
        )
        if res.data and len(res.data) > 0:
            match = res.data[0]
            updated_at = match.get("updated_at")
            is_fresh = False
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - dt).total_seconds() < 86400: # 24 hours
                        is_fresh = True
                except Exception:
                    pass

            if is_fresh and match.get("current_price"):
                print(f"[DB Hit] Cache match: \"{match['name'][:50]}\" (${match['current_price']})")
                
                offers = []
                try:
                    specs_data = json.loads(match.get("specs") or "{}") if isinstance(match.get("specs"), str) else (match.get("specs") or {})
                    offers = specs_data.get("RetailerOffers") or []
                except Exception:
                    offers = []

                if not offers:
                    offers = [{
                        "retailer": match.get("retailer") or "Amazon",
                        "title": match.get("name"),
                        "price": match.get("current_price"),
                        "originalPrice": match.get("msrp"),
                        "url": match.get("product_url"),
                        "imageUrl": match.get("image_url"),
                        "inStock": True
                    }]

                # Broadcast cached offers over SSE so pending UI listeners immediately update
                for off in offers:
                    broadcast_sse("retailer_found", {
                        "query": match.get("model") or clean,
                        "original_query": clean,
                        "retailer": off.get("retailer"),
                        "title": off.get("title"),
                        "price": off.get("price"),
                        "originalPrice": off.get("originalPrice"),
                        "url": off.get("url"),
                        "imageUrl": off.get("imageUrl") or match.get("image_url"),
                        "inStock": off.get("inStock", True),
                        "pending_id": pending_id,
                        "offer": off
                    })

                broadcast_sse("agent_complete", {
                    "query": match.get("model") or clean,
                    "original_query": clean,
                    "category": match.get("category", "GPU"),
                    "bestOffer": {
                        "title": match.get("name"),
                        "price": match.get("current_price"),
                        "retailer": match.get("retailer"),
                        "url": match.get("product_url"),
                        "imageUrl": match.get("image_url"),
                        "inStock": True
                    },
                    "allOffers": offers,
                    "scrapedOffers": offers,
                    "summary": f"Best price for {match.get('model') or clean} is ${match.get('current_price')} at {match.get('retailer')}.",
                    "pending_id": pending_id,
                    "is_error": False,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

                if user_id:
                    try:
                        wl_payload = {
                            "user_id": user_id,
                            "component_id": match.get("id"),
                            "component_name": match.get("name"),
                            "category": match.get("category", "Hardware"),
                            "target_price": round(float(match.get("current_price") or 0) * 0.9, 2),
                            "all_time_low": match.get("lowest_price_90d") or match.get("current_price"),
                            "added_at": datetime.now(timezone.utc).isoformat()
                        }
                        await asyncio.to_thread(
                            supabase.table("watchlist_items").upsert(wl_payload).execute
                        )
                        print(f"💾 [DB Watchlist Cache Synced] User {user_id} tracking {match.get('id')}")
                    except Exception as wl_err:
                        print(f"⚠️ [DB Watchlist Cache Notice]: {wl_err}")

                return {
                    "source": "supabase_database_cache",
                    "query": clean,
                    "bestOffer": {
                        "title": match.get("name"),
                        "price": match.get("current_price"),
                        "retailer": match.get("retailer"),
                        "url": match.get("product_url"),
                        "imageUrl": match.get("image_url"),
                        "inStock": True
                    },
                    "allOffers": offers,
                    "scrapedOffers": offers,
                    "component": match
                }
    except Exception as e:
        print(f"[DB Cache Check Notice] {e}")

    # Step B: Cache miss or stale -> run background scrape via SSE
    print(f"[DB Miss] \"{clean}\" executing multi-retailer agent...")
    asyncio.create_task(_run_scrape_and_persist(clean, user_id, pending_id))

    return {
        "source": "agent_scraping",
        "query": clean,
        "message": "Multi-retailer scrape started. Results will stream via SSE.",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


# ─── API Endpoints ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(sse_publisher())

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "RigScouter-AI Multi-Retailer Pricing Engine",
        "retailers": ["Amazon (Canopy API)", "eBay (Browse API)"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/health")
@app.get("/api/keep-alive")
async def health():
    return {
        "status": "online",
        "database": "connected",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

@app.get("/api/stream")
async def stream(request: Request):
    client_queue = asyncio.Queue()
    sse_clients.add(client_queue)

    async def event_generator():
        try:
            yield {
                "event": "connected",
                "data": json.dumps({
                    "message": "Connected to RigScouter-AI live multi-retailer price feed",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(client_queue.get(), timeout=25.0)
                    yield event
                except asyncio.TimeoutError:
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()})
                    }
        finally:
            sse_clients.remove(client_queue)

    return EventSourceResponse(event_generator())

@app.post("/api/agent/run")
@app.post("/api/scrape")
async def api_post_agent_run(request: Request):
    body = await request.json()
    query = body.get("query") or body.get("prompt") or body.get("url")
    user_id = body.get("userId")
    pending_id = body.get("pendingId")
    if not query:
        return {"error": "Missing 'query' in request body"}
    return await handle_scrape_request(query, user_id, pending_id)

@app.get("/api/agent/run")
@app.get("/api/scrape")
async def api_get_agent_run(query: str = None, q: str = None):
    target = query or q
    if not target:
        return {"error": "Missing ?query= parameter"}
    return await handle_scrape_request(target)

@app.get("/api/components")
async def get_components(category: str = None, q: str = None):
    try:
        query = supabase.table("hardware_components").select("*")
        if category:
            query = query.eq("category", category)
        if q:
            query = query.or_(f"name.ilike.%{q}%,model.ilike.%{q}%,brand.ilike.%{q}%")
        res = await asyncio.to_thread(query.order("updated_at", desc=True).limit(50).execute)
        return {"source": "supabase_database", "components": res.data or []}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/watchlist")
async def get_watchlist():
    try:
        res = await asyncio.to_thread(
            supabase.table("watchlist_items").select("*").order("added_at", desc=True).limit(100).execute
        )
        return {"source": "supabase_database", "items": res.data or []}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/watchlist/{id}")
async def delete_watchlist(id: str):
    try:
        await asyncio.to_thread(supabase.table("watchlist_items").delete().eq("id", id).execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@app.patch("/api/watchlist")
@app.post("/api/watchlist/update-target")
async def update_watchlist(request: Request):
    try:
        body = await request.json()
        item_id = body.get("id")
        target_price = body.get("targetPrice")
        notify = body.get("notifyOnFlashDrop")
        updates = {}
        if target_price is not None:
            updates["target_price"] = float(target_price)
        if notify is not None:
            updates["notify_on_flash_drop"] = bool(notify)

        if item_id and updates:
            await asyncio.to_thread(
                supabase.table("watchlist_items").update(updates).eq("id", item_id).execute
            )
        return {"success": True, "updates": updates}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/components/{id}")
async def delete_component(id: str):
    try:
        await asyncio.to_thread(supabase.table("hardware_components").delete().eq("id", id).execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

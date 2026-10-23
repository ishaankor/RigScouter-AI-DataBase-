from fastapi import APIRouter, Request, Query
from typing import Optional

router = APIRouter(tags=["Agent"])

@router.post("/agent/run")
@router.post("/scrape")
@router.post("/api/agent/run")
@router.post("/api/scrape")
async def api_post_agent_run(request: Request):
    """
    Triggers multi-retailer live agent scrape or returns 24h DB cached prices.
    """
    from server import handle_scrape_request
    body = await request.json()
    query = body.get("query") or body.get("prompt") or body.get("url")
    user_id = body.get("userId")
    pending_id = body.get("pendingId")
    if not query:
        return {"error": "Missing 'query' in request body"}
    return await handle_scrape_request(query, user_id, pending_id)

@router.get("/agent/run")
@router.get("/scrape")
@router.get("/api/agent/run")
@router.get("/api/scrape")
async def api_get_agent_run(query: Optional[str] = None, q: Optional[str] = None):
    """
    GET variant for triggering agent search or price checks.
    """
    from server import handle_scrape_request
    target = query or q
    if not target:
        return {"error": "Missing ?query= parameter"}
    return await handle_scrape_request(target)

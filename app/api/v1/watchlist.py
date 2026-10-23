import asyncio
from fastapi import APIRouter, Request
from app.core.database import supabase

router = APIRouter(tags=["Watchlist"])

@router.get("/watchlist")
@router.get("/api/watchlist")
async def get_watchlist():
    """
    Returns all watchlist items ordered by added_at descending.
    """
    try:
        res = await asyncio.to_thread(
            supabase.table("watchlist_items").select("*").order("added_at", desc=True).limit(100).execute
        )
        return {"source": "supabase_database", "items": res.data or []}
    except Exception as e:
        return {"error": str(e)}

@router.delete("/watchlist/{id}")
@router.delete("/api/watchlist/{id}")
async def delete_watchlist(id: str):
    """
    Deletes a watchlist item by ID.
    """
    try:
        await asyncio.to_thread(supabase.table("watchlist_items").delete().eq("id", id).execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

@router.patch("/watchlist")
@router.patch("/api/watchlist")
@router.post("/watchlist/update-target")
@router.post("/api/watchlist/update-target")
async def update_watchlist(request: Request):
    """
    Updates target_price or notification settings for a watchlist item.
    """
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

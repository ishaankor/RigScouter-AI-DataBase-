import asyncio
from typing import Optional
from fastapi import APIRouter
from app.core.database import supabase

router = APIRouter(tags=["Components"])

@router.get("/components")
@router.get("/api/components")
async def get_components(category: Optional[str] = None, q: Optional[str] = None):
    """
    Returns hardware components filtered optionally by category or search term.
    """
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

@router.delete("/components/{id}")
@router.delete("/api/components/{id}")
async def delete_component(id: str):
    """
    Deletes a hardware component by ID.
    """
    try:
        await asyncio.to_thread(supabase.table("hardware_components").delete().eq("id", id).execute)
        return {"success": True, "deletedId": id}
    except Exception as e:
        return {"error": str(e)}

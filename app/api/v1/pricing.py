from fastapi import APIRouter, Query
from typing import Optional, List
from app.models.offer import PriceCheckResponse
from app.services.scraper_service import scraper_service

router = APIRouter(tags=["Pricing"])

@router.get("/price-check", response_model=PriceCheckResponse)
@router.get("/api/price-check", response_model=PriceCheckResponse)
async def price_check(
    q: str = Query(..., description="Component search term"),
    retailers: Optional[str] = Query(None, description="Comma-separated list of retailers")
):
    selected_retailers = [r.strip() for r in retailers.split(",")] if retailers else None
    offers = await scraper_service.find_all_offers(q, retailers=selected_retailers)
    best = offers[0] if offers else None

    return PriceCheckResponse(
        component_name=q,
        best_price=best.price if best else 0.0,
        best_retailer=best.retailer if best else "None",
        offers=offers
    )

@router.post("/cron/trigger-daily-update")
@router.get("/cron/trigger-daily-update")
@router.post("/api/cron/trigger-daily-update")
@router.get("/api/cron/trigger-daily-update")
async def trigger_daily_update():
    from app.tasks.daily_price_refresh import run_daily_price_refresh_job
    import asyncio
    asyncio.create_task(run_daily_price_refresh_job())
    return {
        "status": "queued",
        "message": "Daily multi-retailer hardware price refresh initiated in background."
    }


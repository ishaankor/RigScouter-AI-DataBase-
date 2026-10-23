from fastapi import APIRouter
from app.api.v1.health import router as health_router
from app.api.v1.pricing import router as pricing_router
from app.api.v1.events import router as events_router
from app.api.v1.digests import router as digests_router
from app.api.v1.components import router as components_router
from app.api.v1.watchlist import router as watchlist_router
from app.api.v1.agent import router as agent_router

v1_router = APIRouter()
v1_router.include_router(health_router)
v1_router.include_router(pricing_router)
v1_router.include_router(events_router)
v1_router.include_router(digests_router)
v1_router.include_router(components_router)
v1_router.include_router(watchlist_router)
v1_router.include_router(agent_router)

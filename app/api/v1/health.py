from fastapi import APIRouter
from datetime import datetime, timezone
from app.core.config import settings

router = APIRouter(tags=["Health"])

@router.get("/health")
@router.get("/")
async def health_check():
    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "version": settings.VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

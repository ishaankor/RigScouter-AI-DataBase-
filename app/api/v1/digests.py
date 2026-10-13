from fastapi import APIRouter, Query, Header, HTTPException
from typing import Optional
from app.core.config import settings
from app.services.digest_service import digest_service

router = APIRouter(tags=["Digests"])

@router.get("/digests/dispatch")
@router.post("/digests/dispatch")
@router.get("/api/cron/dispatch-digest")
@router.post("/api/cron/dispatch-digest")
async def trigger_digest_dispatch(
    key: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
    force: bool = Query(False)
):
    """
    Triggers digest evaluation and delivery for subscribed users.
    Protected by CRON_SECRET.
    """
    token = (authorization or "").replace("Bearer ", "").strip()
    if key != settings.CRON_SECRET and token != settings.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized cron execution request")

    result = await digest_service.dispatch_all_subscribers(force=force)
    return result

import asyncio
from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from app.services.sse_service import sse_service

router = APIRouter(tags=["Events"])

@router.get("/events")
@router.get("/api/events")
@router.get("/stream")
@router.get("/api/stream")
async def events_feed(request: Request):
    """
    Real-time Server-Sent Events endpoint subscribed to price drops & updates.
    """
    client_queue = asyncio.Queue()
    sse_service.clients.add(client_queue)

    async def event_generator():
        try:
            # Immediate welcome ping
            yield {
                "event": "connected",
                "data": '{"status": "connected", "message": "RigScouter real-time feed active"}'
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
                        "data": '{"ping": "heartbeat"}'
                    }
        finally:
            sse_service.clients.discard(client_queue)

    return EventSourceResponse(
        event_generator(),
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache, no-transform"}
    )

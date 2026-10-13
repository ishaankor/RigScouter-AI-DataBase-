import json
import asyncio
import logging
from typing import Set

logger = logging.getLogger("rigscouter.sse")

class SSEService:
    def __init__(self):
        self.clients: Set[asyncio.Queue] = set()
        self.queue: asyncio.Queue = asyncio.Queue()

    async def publisher_loop(self):
        """Infinite loop to pop from internal queue and broadcast to all connected clients."""
        while True:
            try:
                event = await self.queue.get()
                for client_queue in list(self.clients):
                    try:
                        await client_queue.put(event)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SSE Publisher Error] {e}")

    def broadcast(self, event_name: str, data: dict):
        """Places an event onto the broadcast queue."""
        payload = {
            "event": event_name,
            "data": json.dumps(data)
        }
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.queue.put(payload))
        except RuntimeError:
            pass

sse_service = SSEService()

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.api.v1.router import v1_router
from app.services.sse_service import sse_service
from app.tasks.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("rigscouter.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup: Launch background SSE publisher
    sse_task = asyncio.create_task(sse_service.publisher_loop())
    logger.info("📡 SSE event publisher loop started.")

    # 2. Startup: Launch Unified In-App Scheduler
    try:
        start_scheduler()
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

    yield

    # 3. Shutdown
    stop_scheduler()
    sse_task.cancel()
    try:
        await sse_task
    except asyncio.CancelledError:
        pass
    logger.info("🛑 RigScouter engine shutdown complete.")

app = FastAPI(
    title=settings.SERVICE_NAME,
    version=settings.VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount all unified v1 routes (at root and at /api/v1 for backward compatibility)
app.include_router(v1_router)
app.include_router(v1_router, prefix="/api/v1")

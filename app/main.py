"""
Watchman Server - Main Application Entry Point
A deterministic life-state simulator with approval-gated mutations
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys
import asyncio
import httpx

from app.config import get_settings
from app.routes import auth, cycles, commitments, calendar, stats, settings as settings_routes
from app.routes import chat, commands, master_settings
from app.database import init_supabase


# Configure loguru
logger.remove()
logger.add(
    sys.stdout,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


async def keep_alive_ping():
    """
    Background task to ping the server every 4 minutes.
    Prevents Render free tier from sleeping.
    """
    settings = get_settings()
    
    # Only run in production
    if settings.app_env != "production":
        logger.info("Keep-alive disabled in non-production environment")
        return
    
    # Wait 30 seconds for server to fully start
    await asyncio.sleep(30)
    
    ping_url = "https://watchman-api-dnm0.onrender.com/health"
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(ping_url, timeout=10)
                logger.debug(f"Keep-alive ping: {response.status_code}")
            except Exception as e:
                logger.warning(f"Keep-alive ping failed: {e}")
            
            # Sleep for 4 minutes (240 seconds)
            await asyncio.sleep(240)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    logger.info("Starting Watchman Server...")
    
    # Initialize Supabase client
    init_supabase()
    logger.info("Supabase client initialized")
    
    # Start keep-alive background task
    keep_alive_task = asyncio.create_task(keep_alive_ping())
    logger.info("Keep-alive task started (pings every 4 mins)")
    
    yield
    
    # Cancel keep-alive on shutdown
    keep_alive_task.cancel()
    logger.info("Shutting down Watchman Server...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application"""
    settings = get_settings()
    
    app = FastAPI(
        title="Watchman API",
        description="A deterministic life-state simulator with approval-gated mutations. Guard your hours. Live by rule, not noise.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",  # Always enabled - endpoints require auth anyway
        redoc_url="/redoc",
    )
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Include routers
    app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
    app.include_router(cycles.router, prefix="/api/cycles", tags=["Cycles"])
    app.include_router(commitments.router, prefix="/api/commitments", tags=["Commitments"])
    app.include_router(calendar.router, prefix="/api/calendar", tags=["Calendar"])
    app.include_router(stats.router, prefix="/api/stats", tags=["Statistics"])
    app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
    
    # New conversation-first routes
    app.include_router(chat.router, prefix="/api", tags=["Chat"])
    app.include_router(commands.router, prefix="/api", tags=["Commands"])
    app.include_router(master_settings.router, prefix="/api", tags=["Master Settings"])
    
    @app.get("/", tags=["Health"])
    async def root():
        """Health check endpoint"""
        return {
            "service": "Watchman API",
            "status": "operational",
            "tagline": "Time under control."
        }
    
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Detailed health check"""
        return {
            "status": "healthy",
            "environment": settings.app_env,
            "version": "1.0.0"
        }
    
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug
    )

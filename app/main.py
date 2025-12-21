"""
Watchman Server - Main Application Entry Point
A deterministic life-state simulator with approval-gated mutations
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
import sys

from app.config import get_settings
from app.routes import auth, cycles, commitments, calendar, mutations, proposals, stats, settings as settings_routes
from app.database import init_supabase


# Configure loguru
logger.remove()
logger.add(
    sys.stdout,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    logger.info("Starting Watchman Server...")
    
    # Initialize Supabase client
    init_supabase()
    logger.info("Supabase client initialized")
    
    yield
    
    logger.info("Shutting down Watchman Server...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application"""
    settings = get_settings()
    
    app = FastAPI(
        title="Watchman API",
        description="A deterministic life-state simulator with approval-gated mutations. Guard your hours. Live by rule, not noise.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
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
    app.include_router(mutations.router, prefix="/api/mutations", tags=["Mutations"])
    app.include_router(proposals.router, prefix="/api/proposals", tags=["Proposals"])
    app.include_router(stats.router, prefix="/api/stats", tags=["Statistics"])
    app.include_router(settings_routes.router, prefix="/api/settings", tags=["Settings"])
    
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

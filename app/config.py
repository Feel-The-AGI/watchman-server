"""
Watchman Server Configuration
Centralized settings management using Pydantic
"""

from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Supabase - Required for full functionality
    supabase_url: str = "https://vooqsegebpcipjrhcxsy.supabase.co"
    supabase_anon_key: str = ""  # Set via SUPABASE_ANON_KEY env var
    supabase_service_key: str = ""  # Set via SUPABASE_SERVICE_KEY env var  
    supabase_jwt_secret: str = ""
    
    # Gemini AI
    gemini_api_key: str = ""
    
    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id_pro: str = ""
    
    # Application
    app_env: str = "development"
    debug: bool = True
    cors_origins: str = "https://trywatchman.app,https://www.trywatchman.app,https://trywatchman.vercel.app,https://watchman-client.vercel.app"
    
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    
    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.cors_origins.split(",")]
    
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()

"""
Watchman Server Configuration
Centralized settings management using Pydantic
"""

from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Supabase - Required for full functionality. No default project URL.
    supabase_url: str = ""
    supabase_anon_key: str = ""  # Set via SUPABASE_ANON_KEY env var
    supabase_service_key: str = ""  # Set via SUPABASE_SERVICE_KEY env var
    supabase_jwt_secret: str = ""

    # Gemini AI
    gemini_api_key: str = ""

    # Paystack. Webhook signatures use paystack_secret_key.
    paystack_secret_key: str = ""  # Set via PAYSTACK_SECRET_KEY env var
    paystack_public_key: str = ""  # Set via PAYSTACK_PUBLIC_KEY env var
    paystack_webhook_secret: str = ""  # Legacy unused setting
    
    # Exchange Rate API (for USD to GHS conversion)
    exchange_rate_api_key: str = ""  # Set via EXCHANGE_RATE_API_KEY env var
    pro_price_usd: float = 12.0  # Pro subscription price in USD

    # Email (Resend)
    resend_api_key: str = ""
    email_from: str = "Watchman <notifications@trywatchman.app>"

    # Shared secret for /api/cron/*. Required in every deployed environment.
    cron_secret: str = ""

    # Application
    app_env: str = "development"
    debug: bool = False
    cors_origins: str = "https://trywatchman.app,https://www.trywatchman.app,https://watchman.app,https://www.watchman.app,https://trywatchman.vercel.app,https://watchman-client.vercel.app"
    
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

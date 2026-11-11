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

    # Anthropic Claude. Haiku 5.5 is the default chat model.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-5-5"
    # Adaptive thinking tokens count toward max_tokens. The previous chat
    # call used adaptive thinking with max_tokens 8192 and spent 25-30s.
    # "disabled" skips that hidden reasoning. "adaptive" keeps it and uses
    # anthropic_thinking_effort so the budget stays small.
    anthropic_thinking: str = "disabled"
    anthropic_thinking_effort: str = "low"
    anthropic_chat_max_tokens: int = 2048
    anthropic_timeout_seconds: float = 30

    # Paystack. Webhook signatures use paystack_secret_key.
    paystack_secret_key: str = ""  # Set via PAYSTACK_SECRET_KEY env var
    paystack_public_key: str = ""  # Set via PAYSTACK_PUBLIC_KEY env var
    paystack_webhook_secret: str = ""  # Legacy unused setting

    # Stripe card subscriptions. Empty keys make /api/billing return 503.
    # Price values are Stripe Price ids (price_...), not dollar amounts.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_monthly: str = ""
    stripe_price_yearly: str = ""
    stripe_price_founding: str = ""
    # Empty uses Stripe's API. Local tests point this at stripe-mock.
    stripe_api_base: str = ""
    # Checkout success, cancel, and the Customer Portal return here.
    client_base_url: str = "https://trywatchman.app"
    
    # Exchange Rate API (for USD to GHS conversion)
    exchange_rate_api_key: str = ""  # Set via EXCHANGE_RATE_API_KEY env var
    pro_price_usd: float = 12.0  # Pro subscription price in USD

    # Email (Resend)
    resend_api_key: str = ""
    email_from: str = "Watchman <notifications@trywatchman.app>"

    # Shared secret for /api/cron/*. Required in every deployed environment.
    cron_secret: str = ""

    # Trial. TRIAL_ENABLED false means no new trials: an account created
    # at or after the cutoff never starts one, and an earlier free account
    # keeps the rest of its 3 days. TRIAL_END_ALL is the only switch that
    # ends a trial already underway, and it defaults to off.
    trial_enabled: bool = False
    trial_signup_cutoff: str = "2026-10-22T00:00:00Z"
    trial_end_all: bool = False

    # Prompt versions. Empty means the latest stable version.
    # CHAT_PROMPT_VERSION=chat-v1 rolls back with no code change.
    # CHAT_PROMPT_ALLOWLIST is user-id:version pairs, comma separated,
    # and only those accounts leave the active version.
    chat_prompt_version: str = ""
    onboarding_prompt_version: str = ""
    chat_prompt_allowlist: str = ""
    onboarding_prompt_allowlist: str = ""

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

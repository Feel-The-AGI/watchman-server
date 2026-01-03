"""
Watchman Authentication Middleware
Validates Supabase JWT tokens and extracts user information
"""

import httpx
from typing import Optional
from datetime import datetime, timedelta, timezone
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from loguru import logger

from app.config import get_settings
from app.database import Database
from app.services.email_service import get_email_service


# Trial period configuration
TRIAL_DURATION_DAYS = 3

# Cache for IP geolocation to avoid repeated API calls
_ip_geo_cache = {}


security = HTTPBearer(auto_error=False)


async def get_ip_geolocation(ip: str) -> dict:
    """
    Get geolocation data for an IP address using ip-api.com (free, no key needed).
    Caches results to avoid repeated API calls.
    """
    # Skip local/private IPs
    if not ip or ip.startswith(('127.', '192.168.', '10.', '172.')) or ip == '::1':
        return {}
    
    # Check cache
    if ip in _ip_geo_cache:
        return _ip_geo_cache[ip]
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,timezone",
                timeout=3.0
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    geo_data = {
                        "country": data.get("country"),
                        "country_code": data.get("countryCode"),
                        "region": data.get("regionName"),
                        "city": data.get("city"),
                        "timezone": data.get("timezone"),
                    }
                    _ip_geo_cache[ip] = geo_data
                    logger.debug(f"[GEO] Got location for {ip}: {geo_data.get('country')}")
                    return geo_data
    except Exception as e:
        logger.debug(f"[GEO] Failed to get location for {ip}: {e}")
    
    return {}


class AuthMiddleware:
    """JWT authentication middleware for Supabase tokens"""
    
    def __init__(self):
        self.settings = get_settings()
    
    def verify_token(self, token: str) -> Optional[dict]:
        """Verify a Supabase JWT token"""
        try:
            # Supabase tokens use HS256 with the JWT secret
            payload = jwt.decode(
                token,
                self.settings.supabase_jwt_secret,
                algorithms=["HS256"],
                audience="authenticated"
            )
            return payload
        except JWTError as e:
            logger.warning(f"JWT verification failed: {e}")
            return None
    
    def extract_user_id(self, payload: dict) -> Optional[str]:
        """Extract the user ID (sub claim) from token payload"""
        return payload.get("sub")


auth_middleware = AuthMiddleware()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """
    Dependency to get the current authenticated user.
    Auto-creates user in database if they exist in Supabase Auth but not in users table.
    """
    logger.info(f"[AUTH] get_current_user called - Path: {request.url.path}, Method: {request.method}")

    if credentials is None:
        logger.warning(f"[AUTH] No credentials provided for {request.url.path}")
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}
        )

    token = credentials.credentials
    logger.debug(f"[AUTH] Token received (first 20 chars): {token[:20]}...")

    # Verify the token
    payload = auth_middleware.verify_token(token)

    if payload is None:
        logger.warning(f"[AUTH] Token verification failed for {request.url.path}")
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # Get the user ID from token
    auth_id = auth_middleware.extract_user_id(payload)
    logger.info(f"[AUTH] Token verified - auth_id: {auth_id}")

    if not auth_id:
        logger.error(f"[AUTH] No auth_id in token payload")
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload"
        )

    # Fetch the user from database
    logger.debug(f"[AUTH] Fetching user from database - auth_id: {auth_id}")
    db = Database(use_admin=True)
    user = await db.get_user_by_auth_id(auth_id)
    
    # Get client IP for geolocation
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.client.host if request.client else None

    if not user:
        # Auto-create user from Supabase Auth data
        logger.info(f"[AUTH] User not found, auto-creating for auth_id: {auth_id}")

        # Extract user info from JWT payload
        user_metadata = payload.get("user_metadata", {})
        email = payload.get("email") or user_metadata.get("email", "")
        name = user_metadata.get("full_name") or user_metadata.get("name") or email.split("@")[0]

        logger.info(f"[AUTH] Creating new user - email: {email}, name: {name}")
        
        # Get geolocation for new user
        geo_data = await get_ip_geolocation(client_ip) if client_ip else {}

        # Create user in database with location
        user = await db.create_user({
            "auth_id": auth_id,
            "email": email,
            "name": name,
            "tier": "free",
            "role": "user",
            "onboarding_completed": False,
            "settings": {"max_concurrent_commitments": 2},
            "country": geo_data.get("country"),
            "country_code": geo_data.get("country_code"),
            "region": geo_data.get("region"),
            "city": geo_data.get("city"),
            "timezone": geo_data.get("timezone"),
            "last_ip": client_ip,
            "last_active": datetime.utcnow().isoformat(),
        })

        if not user:
            logger.error(f"[AUTH] Failed to create user for auth_id: {auth_id}")
            raise HTTPException(
                status_code=500,
                detail="Failed to create user account"
            )

        logger.info(f"[AUTH] User created successfully: {user.get('id')} ({email}) from {geo_data.get('country', 'Unknown')}")

        # Send welcome email to new user
        try:
            email_service = get_email_service()
            await email_service.send_welcome_email(
                to=email,
                user_name=name,
            )
            logger.info(f"[AUTH] Welcome email sent to {email}")
        except Exception as e:
            # Don't fail user creation if email fails
            logger.warning(f"[AUTH] Failed to send welcome email to {email}: {e}")
    else:
        logger.info(f"[AUTH] User found: {user.get('id')} ({user.get('email')}) - tier: {user.get('tier', 'free')}")
        
        # Update last_active and location if missing
        update_data = {"last_active": datetime.utcnow().isoformat()}
        
        # Update location if not set
        if not user.get("country") and client_ip:
            geo_data = await get_ip_geolocation(client_ip)
            if geo_data:
                update_data.update({
                    "country": geo_data.get("country"),
                    "country_code": geo_data.get("country_code"),
                    "region": geo_data.get("region"),
                    "city": geo_data.get("city"),
                    "timezone": geo_data.get("timezone"),
                    "last_ip": client_ip,
                })
                logger.info(f"[AUTH] Updated user location: {geo_data.get('country')}")
        
        # Update in background (don't await to keep auth fast)
        try:
            await db.update_user(user["id"], update_data)
        except Exception as e:
            logger.warning(f"[AUTH] Failed to update last_active: {e}")

    return user


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> Optional[dict]:
    """
    Dependency to optionally get the current user.
    Returns None if not authenticated.
    """
    if credentials is None:
        return None
    
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None


async def require_pro_tier(
    user: dict = Depends(get_current_user)
) -> dict:
    """Dependency to require Pro tier or higher"""
    tier = user.get("tier", "free")
    
    if tier not in ["pro", "admin"]:
        logger.info(f"Pro feature blocked for user {user.get('id')} (tier: {tier})")
        raise HTTPException(
            status_code=403,
            detail="This feature is part of the Pro plan. Upgrade to unlock intelligent parsing and full statistics."
        )
    
    return user


async def require_admin(
    user: dict = Depends(get_current_user)
) -> dict:
    """Dependency to require admin role"""
    role = user.get("role", "user")
    tier = user.get("tier", "free")

    if role != "admin" and tier != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )

    return user


def is_in_trial(user: dict) -> bool:
    """Check if user is within their 3-day trial period"""
    # Only free tier users can be in trial
    if user.get("tier") not in ["free", None]:
        return False

    created_at = user.get("created_at")
    if not created_at:
        return False

    try:
        # Parse the created_at timestamp
        if isinstance(created_at, str):
            # Handle ISO format with timezone
            created_at = created_at.replace('Z', '+00:00')
            created_date = datetime.fromisoformat(created_at)
        else:
            created_date = created_at

        # Ensure timezone awareness
        if created_date.tzinfo is None:
            created_date = created_date.replace(tzinfo=timezone.utc)

        trial_end = created_date + timedelta(days=TRIAL_DURATION_DAYS)
        now = datetime.now(timezone.utc)

        is_trial = now < trial_end
        if is_trial:
            logger.info(f"[AUTH] User {user.get('id')} is in trial (ends: {trial_end.isoformat()})")

        return is_trial
    except Exception as e:
        logger.warning(f"[AUTH] Error checking trial status: {e}")
        return False


def get_effective_tier(user: dict) -> str:
    """
    Get the effective tier for a user, considering trial status.
    Returns 'trial' if user is in trial period, otherwise their actual tier.
    """
    actual_tier = user.get("tier", "free")

    if actual_tier in ["pro", "admin"]:
        return actual_tier

    if is_in_trial(user):
        return "trial"

    return actual_tier


async def require_pro_or_trial(
    user: dict = Depends(get_current_user)
) -> dict:
    """
    Dependency to require Pro tier OR trial period.
    Use this for features that should be available during trial.
    For exports (no trial access), use require_pro_tier instead.
    """
    effective_tier = get_effective_tier(user)

    if effective_tier not in ["pro", "admin", "trial"]:
        logger.info(f"Pro feature blocked for user {user.get('id')} (tier: {user.get('tier')})")
        raise HTTPException(
            status_code=403,
            detail="This feature is part of the Pro plan. Upgrade to unlock intelligent parsing and full statistics."
        )

    return user

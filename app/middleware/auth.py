"""
Watchman Authentication Middleware
Validates Supabase JWT tokens and extracts user information
"""

from typing import Optional
from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from loguru import logger

from app.config import get_settings
from app.database import Database


security = HTTPBearer(auto_error=False)


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

    if not user:
        # Auto-create user from Supabase Auth data
        logger.info(f"[AUTH] User not found, auto-creating for auth_id: {auth_id}")

        # Extract user info from JWT payload
        user_metadata = payload.get("user_metadata", {})
        email = payload.get("email") or user_metadata.get("email", "")
        name = user_metadata.get("full_name") or user_metadata.get("name") or email.split("@")[0]

        logger.info(f"[AUTH] Creating new user - email: {email}, name: {name}")

        # Create user in database
        user = await db.create_user({
            "auth_id": auth_id,
            "email": email,
            "name": name,
            "tier": "free",
            "role": "user",
            "onboarding_completed": False,
            "settings": {"max_concurrent_commitments": 2}
        })

        if not user:
            logger.error(f"[AUTH] Failed to create user for auth_id: {auth_id}")
            raise HTTPException(
                status_code=500,
                detail="Failed to create user account"
            )

        logger.info(f"[AUTH] User created successfully: {user.get('id')} ({email})")
    else:
        logger.info(f"[AUTH] User found: {user.get('id')} ({user.get('email')}) - tier: {user.get('tier', 'free')}")

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

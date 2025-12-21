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
    Raises HTTPException if not authenticated.
    """
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = credentials.credentials
    
    # Verify the token
    payload = auth_middleware.verify_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    # Get the user ID from token
    auth_id = auth_middleware.extract_user_id(payload)
    
    if not auth_id:
        raise HTTPException(
            status_code=401,
            detail="Invalid token payload"
        )
    
    # Fetch the user from database
    db = Database(use_admin=True)
    user = await db.get_user_by_auth_id(auth_id)
    
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )
    
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
        raise HTTPException(
            status_code=403,
            detail="This feature requires a Pro subscription"
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

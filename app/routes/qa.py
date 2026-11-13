"""Temporary QA accounts with a pre-confirmed email.

Off unless QA_BYPASS_SECRET (32+ characters), QA_EMAIL_PREFIX, and
QA_EMAIL_DOMAIN are all set: every route then answers 404. When on, a caller
with the secret can create or delete an account only for an address of the
form ``<prefix><letters and digits>@<domain>``, for example
``owner+qa01@gmail.com``. No other account can be created, confirmed,
changed, or deleted here. Unset the variables to turn it off again.
"""

import hmac
import re
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from loguru import logger
from pydantic import BaseModel

from app.config import get_settings
from app.database import Database, get_supabase_admin

router = APIRouter()

MIN_SECRET_LENGTH = 32
_SUFFIX = re.compile(r"^[a-z0-9]{1,24}$")


def qa_enabled(settings) -> bool:
    return (
        len(settings.qa_bypass_secret or "") >= MIN_SECRET_LENGTH
        and bool((settings.qa_email_prefix or "").strip())
        and bool((settings.qa_email_domain or "").strip())
    )


def allowed_qa_email(email: str, prefix: str, domain: str) -> bool:
    """True only for <prefix><a-z0-9, 1 to 24><@domain>, compared in lower case."""
    email = (email or "").strip().lower()
    prefix = (prefix or "").strip().lower()
    domain = (domain or "").strip().lower().lstrip("@")
    if not prefix or not domain or email.count("@") != 1:
        return False
    local, _, host = email.partition("@")
    if host != domain or not local.startswith(prefix):
        return False
    return bool(_SUFFIX.match(local[len(prefix):]))


def _guard(secret: Optional[str], email: str) -> str:
    settings = get_settings()
    if not qa_enabled(settings):
        raise HTTPException(status_code=404, detail="Not Found")
    if not secret or not hmac.compare_digest(secret, settings.qa_bypass_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not allowed_qa_email(email, settings.qa_email_prefix, settings.qa_email_domain):
        raise HTTPException(status_code=403, detail="That address is not a QA address")
    return email.strip().lower()


class QaAccountRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None
    tier: Literal["free", "pro"] = "free"


class QaAccountDelete(BaseModel):
    email: str


@router.post("/accounts")
async def create_qa_account(request: QaAccountRequest, x_qa_secret: str = Header(None)):
    email = _guard(x_qa_secret, request.email)
    if len(request.password or "") < 10:
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters")
    admin = get_supabase_admin()
    try:
        created = admin.auth.admin.create_user({
            "email": email,
            "password": request.password,
            "email_confirm": True,
            "user_metadata": {"name": request.name or "Watchman QA"},
        })
    except Exception as exc:
        logger.warning(f"[QA] Could not create {email}: {exc}")
        raise HTTPException(status_code=409, detail="Account could not be created. It may already exist.")
    auth_id = getattr(getattr(created, "user", None), "id", None)
    if request.tier == "pro":
        db = Database(use_admin=True)
        profile = await db.get_user_by_email(email)
        if not profile:
            raise HTTPException(status_code=500, detail="Account created, but its profile row is missing")
        await db.update_user(profile["id"], {"tier": "pro"})
    logger.warning(f"[QA] Created confirmed QA account {email} ({request.tier})")
    return {"email": email, "auth_id": str(auth_id) if auth_id else None, "tier": request.tier}


@router.delete("/accounts")
async def delete_qa_account(request: QaAccountDelete, x_qa_secret: str = Header(None)):
    email = _guard(x_qa_secret, request.email)
    db = Database(use_admin=True)
    profile = await db.get_user_by_email(email)
    if not profile:
        raise HTTPException(status_code=404, detail="No such QA account")
    await db.delete_all_user_data(profile["id"])
    removed = await db.delete_auth_user(profile["auth_id"]) if profile.get("auth_id") else False
    logger.warning(f"[QA] Deleted QA account {email} (auth removed: {removed})")
    return {"email": email, "deleted": True, "auth_removed": bool(removed)}

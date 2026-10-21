"""Live admin accounts use tier='admin' and role='user'. Both gates must allow that."""

import pytest

from app.middleware.auth import require_admin as middleware_require_admin
from app.routes.admin import require_admin as route_require_admin
from fastapi import HTTPException


JASON = {"id": "admin-user", "tier": "admin", "role": "user"}


@pytest.mark.asyncio
async def test_tier_admin_keeps_access_when_role_is_user():
    assert await middleware_require_admin(JASON) == JASON
    assert route_require_admin(JASON) == JASON


@pytest.mark.asyncio
async def test_role_alone_does_not_open_admin_routes():
    user = {"id": "role-only", "tier": "free", "role": "admin"}
    assert await middleware_require_admin(user) == user
    with pytest.raises(HTTPException) as exc:
        route_require_admin(user)
    assert exc.value.status_code == 403

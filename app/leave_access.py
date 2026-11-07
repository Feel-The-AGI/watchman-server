"""Leave planning is on every plan, Free included.

The pricing page and the terms promise leave planning on Free. Free accounts
still plan only inside their rolling three-month horizon, which
``app.planning_horizon`` enforces for leave the same way it does for every
other dated change. ``leave_blocked`` stays so callers keep one place to ask.
"""

LEAVE_PLAN_TIERS = {"free", "pro", "admin", "trial"}
# Kept for callers that still import it. No plan is refused leave today.
LEAVE_PRO_REPLY = (
    "Leave planning is a Pro feature. Upgrade to Pro to plan vacation and time off."
)
_LEAVE_ACTIONS = {"add_leave", "remove_leave", "update_leave"}


def leave_planning_request(action: str, payload: dict) -> bool:
    """True for vacation or other leave edits. Sick leave counts as well."""
    payload = payload if isinstance(payload, dict) else {}
    kind = str(payload.get("type") or "").strip().lower()
    work = str(payload.get("work_type") or "").strip().lower()
    if action in _LEAVE_ACTIONS:
        return True
    if action in {"add_commitment", "remove_commitment"} and kind == "leave":
        return True
    if action == "override_days" and work == "leave":
        return True
    return False


def leave_blocked(tier: str, action: str, payload: dict) -> bool:
    """Always False: leave planning is not a paid feature."""
    return False

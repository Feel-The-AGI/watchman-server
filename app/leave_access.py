"""Leave planning is Pro, trial, or admin. Sick days are not leave planning."""

PAID_LEAVE_TIERS = {"pro", "admin", "trial"}
LEAVE_PRO_REPLY = (
    "Leave planning is a Pro feature. Upgrade to Pro to plan vacation and time off."
)
_LEAVE_ACTIONS = {"add_leave", "remove_leave", "update_leave"}


def leave_planning_request(action: str, payload: dict) -> bool:
    """True for vacation or other leave edits. Sick leave stays available."""
    payload = payload if isinstance(payload, dict) else {}
    kind = str(payload.get("type") or "").strip().lower()
    work = str(payload.get("work_type") or "").strip().lower()
    if action in _LEAVE_ACTIONS:
        return kind != "sick"
    if action in {"add_commitment", "remove_commitment"} and kind == "leave":
        return True
    if action == "override_days" and work == "leave":
        return True
    return False


def leave_blocked(tier: str, action: str, payload: dict) -> bool:
    if (tier or "") in PAID_LEAVE_TIERS:
        return False
    if tier != "free":
        return False
    return leave_planning_request(action, payload)

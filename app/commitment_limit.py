"""The free plan stores two commitments. A reused row does not count as another."""

FREE_COMMITMENT_LIMIT = 2
COMMITMENT_LIMIT_REPLY = (
    "More commitments are a Pro feature. Upgrade to Pro to track more than 2."
)


def free_commitment_limit_hit(tier: str, existing_rows: list, reused) -> bool:
    """Same gate as POST /commitments: free, no reused row, and already at the cap."""
    return tier == "free" and reused is None and len(existing_rows or []) >= FREE_COMMITMENT_LIMIT


def reused_commitment(rows: list, user_id: str, payload: dict):
    """The row this add would keep, or None when it would insert a new one.

    An id already stored for this user is the HTTP reuse rule. The same name,
    start date, and type is the chat rule that does not insert a second row.
    """
    payload = payload if isinstance(payload, dict) else {}
    supplied = str(payload.get("id") or "").strip()
    if supplied:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "") != supplied:
                continue
            owner = row.get("user_id")
            if owner in (None, user_id):
                return row
    name = str(payload.get("name") or "Commitment").strip() or "Commitment"
    start = str(payload.get("start_date") or payload.get("date") or "")[:10]
    kind = str(payload.get("type") or "personal").strip().lower()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if (
            row.get("name") == name
            and str(row.get("start_date") or "")[:10] == start
            and row.get("type") == kind
        ):
            return row
    return None

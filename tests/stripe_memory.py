"""In-memory stand-in for the Stripe billing queries.

The unit tests and the local end-to-end script use this so they can exercise
the FastAPI routes without a Supabase project. Production uses Database.
"""


class MemoryBillingDB:
    def __init__(self):
        self.users = {}
        self.subs = {}
        self.seats = {}
        self.events = {}
        self.ready = True

    async def stripe_billing_ready(self) -> bool:
        return self.ready

    async def get_user_by_id(self, user_id: str):
        row = self.users.get(user_id)
        return dict(row) if row else None

    async def update_user_billing(self, user_id: str, data: dict) -> dict:
        if user_id not in self.users:
            raise RuntimeError(f"missing user {user_id}")
        self.users[user_id].update(data)
        return dict(self.users[user_id])

    async def get_user_by_stripe_customer(self, customer_id: str):
        if not customer_id:
            return None
        for user in self.users.values():
            if user.get("stripe_customer_id") == customer_id:
                return dict(user)
        return None

    async def get_user_by_stripe_subscription(self, subscription_id: str):
        if not subscription_id:
            return None
        for user in self.users.values():
            if user.get("stripe_subscription_id") == subscription_id:
                return dict(user)
        return None

    async def get_stripe_subscription_row(self, user_id: str):
        row = self.subs.get(user_id)
        return dict(row) if row else None

    async def save_stripe_subscription(self, user_id: str, fields: dict) -> dict:
        row = dict(self.subs.get(user_id) or {"id": f"subrow-{user_id}", "user_id": user_id})
        row.update(fields)
        row["user_id"] = user_id
        self.subs[user_id] = row
        return dict(row)

    async def count_founding_seats(self) -> int:
        return len(self.seats)

    async def user_has_founding_seat(self, user_id: str) -> bool:
        return user_id in self.seats

    async def record_founding_seat(self, user_id: str, subscription_id, status: str) -> str:
        if user_id not in self.seats and len(self.seats) >= 100:
            return "full"
        existed = user_id in self.seats
        self.seats[user_id] = {
            "user_id": user_id,
            "stripe_subscription_id": subscription_id,
            "status": status,
        }
        return "exists" if existed else "recorded"

    async def get_stripe_event(self, event_id: str):
        row = self.events.get(event_id)
        return dict(row) if row else None

    async def insert_stripe_event(self, event_id: str, event_type: str) -> bool:
        if event_id in self.events:
            return False
        self.events[event_id] = {"id": event_id, "event_type": event_type}
        return True

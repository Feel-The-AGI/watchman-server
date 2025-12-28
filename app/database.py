"""
Watchman Database Module
Supabase client initialization and connection management
"""

from typing import Optional
from supabase import create_client, Client
from loguru import logger

from app.config import get_settings

_supabase_client: Optional[Client] = None
_supabase_admin_client: Optional[Client] = None


def init_supabase() -> None:
    """Initialize Supabase clients"""
    global _supabase_client, _supabase_admin_client
    
    settings = get_settings()
    
    # Regular client with anon key (respects RLS)
    _supabase_client = create_client(
        settings.supabase_url,
        settings.supabase_anon_key
    )
    
    # Admin client with service role key (bypasses RLS)
    _supabase_admin_client = create_client(
        settings.supabase_url,
        settings.supabase_service_key
    )
    
    logger.info("Supabase clients initialized successfully")


def get_supabase() -> Client:
    """Get the regular Supabase client (with RLS)"""
    if _supabase_client is None:
        init_supabase()
    return _supabase_client


def get_supabase_admin() -> Client:
    """Get the admin Supabase client (bypasses RLS)"""
    if _supabase_admin_client is None:
        init_supabase()
    return _supabase_admin_client


class Database:
    """Database operations wrapper for Supabase"""

    def __init__(self, use_admin: bool = False):
        self.client = get_supabase_admin() if use_admin else get_supabase()
        self.use_admin = use_admin
        logger.debug(f"[DB] Database instance created (admin: {use_admin})")

    # ==========================================
    # Users
    # ==========================================

    async def get_user_by_auth_id(self, auth_id: str) -> Optional[dict]:
        """Get user by Supabase auth ID"""
        logger.debug(f"[DB] get_user_by_auth_id: {auth_id}")
        try:
            result = self.client.table("users").select("*").eq("auth_id", auth_id).single().execute()
            if result.data:
                logger.debug(f"[DB] User found: {result.data.get('id')}")
            else:
                logger.debug(f"[DB] No user found for auth_id: {auth_id}")
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting user by auth_id {auth_id}: {e}")
            return None

    async def create_user(self, data: dict) -> Optional[dict]:
        """Create a new user"""
        logger.info(f"[DB] create_user: {data.get('email')}")
        try:
            result = self.client.table("users").insert(data).execute()
            if result.data:
                logger.info(f"[DB] User created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating user: {e}")
            return None

    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        """Get user by internal ID"""
        logger.debug(f"[DB] get_user_by_id: {user_id}")
        try:
            result = self.client.table("users").select("*").eq("id", user_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting user by id {user_id}: {e}")
            return None

    async def update_user(self, user_id: str, data: dict) -> dict:
        """Update user data"""
        logger.info(f"[DB] update_user: {user_id} - fields: {list(data.keys())}")
        try:
            result = self.client.table("users").update(data).eq("id", user_id).execute()
            logger.debug(f"[DB] User updated: {user_id}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating user {user_id}: {e}")
            return None

    async def complete_onboarding(self, user_id: str) -> dict:
        """Mark user onboarding as completed"""
        logger.info(f"[DB] complete_onboarding: {user_id}")
        return await self.update_user(user_id, {"onboarding_completed": True})

    async def get_user_by_stripe_customer(self, stripe_customer_id: str) -> Optional[dict]:
        """Get user by Stripe customer ID"""
        logger.debug(f"[DB] get_user_by_stripe_customer: {stripe_customer_id}")
        try:
            result = self.client.table("users").select("*").eq("stripe_customer_id", stripe_customer_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting user by stripe_customer_id {stripe_customer_id}: {e}")
            return None

    async def create_payment_record(self, data: dict) -> Optional[dict]:
        """Create a payment record"""
        logger.info(f"[DB] create_payment_record: user={data.get('user_id')}, amount={data.get('amount')}")
        try:
            result = self.client.table("payments").insert(data).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating payment record: {e}")
            return None

    async def get_payment_history(self, user_id: str) -> list:
        """Get user's payment history"""
        logger.debug(f"[DB] get_payment_history: {user_id}")
        try:
            result = self.client.table("payments").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
            return result.data if result.data else []
        except Exception as e:
            logger.error(f"[DB] Error getting payment history for {user_id}: {e}")
            return []
    
    # ==========================================
    # Cycles
    # ==========================================

    async def get_cycles(self, user_id: str) -> list:
        """Get all cycles for a user"""
        logger.debug(f"[DB] get_cycles: user_id={user_id}")
        try:
            result = self.client.table("cycles").select("*").eq("user_id", user_id).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} cycles")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting cycles: {e}")
            return []

    async def get_active_cycle(self, user_id: str) -> Optional[dict]:
        """Get the active cycle for a user"""
        logger.debug(f"[DB] get_active_cycle: user_id={user_id}")
        try:
            result = self.client.table("cycles").select("*").eq("user_id", user_id).eq("is_active", True).single().execute()
            if result.data:
                logger.debug(f"[DB] Active cycle found: {result.data.get('id')}")
            else:
                logger.debug(f"[DB] No active cycle found for user {user_id}")
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting active cycle: {e}")
            return None

    async def create_cycle(self, data: dict) -> dict:
        """Create a new cycle"""
        logger.info(f"[DB] create_cycle: user_id={data.get('user_id')}, name={data.get('name')}")
        try:
            result = self.client.table("cycles").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Cycle created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating cycle: {e}")
            return None

    async def update_cycle(self, cycle_id: str, data: dict) -> dict:
        """Update a cycle"""
        logger.info(f"[DB] update_cycle: {cycle_id} - fields: {list(data.keys())}")
        try:
            result = self.client.table("cycles").update(data).eq("id", cycle_id).execute()
            logger.debug(f"[DB] Cycle updated: {cycle_id}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating cycle: {e}")
            return None

    async def delete_cycle(self, cycle_id: str) -> bool:
        """Delete a cycle"""
        logger.info(f"[DB] delete_cycle: {cycle_id}")
        try:
            self.client.table("cycles").delete().eq("id", cycle_id).execute()
            logger.debug(f"[DB] Cycle deleted: {cycle_id}")
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting cycle: {e}")
            return False
    
    # ==========================================
    # Constraints
    # ==========================================

    async def get_constraints(self, user_id: str) -> list:
        """Get all constraints for a user"""
        logger.debug(f"[DB] get_constraints: user_id={user_id}")
        try:
            result = self.client.table("constraints").select("*").eq("user_id", user_id).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} constraints")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting constraints: {e}")
            return []

    async def get_active_constraints(self, user_id: str) -> list:
        """Get only active constraints for a user"""
        logger.debug(f"[DB] get_active_constraints: user_id={user_id}")
        try:
            result = self.client.table("constraints").select("*").eq("user_id", user_id).eq("is_active", True).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} active constraints")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting active constraints: {e}")
            return []

    async def create_constraint(self, data: dict) -> dict:
        """Create a new constraint"""
        logger.info(f"[DB] create_constraint: name={data.get('name')}")
        try:
            result = self.client.table("constraints").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Constraint created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating constraint: {e}")
            return None

    async def create_default_constraints(self, user_id: str) -> list:
        """Create default system constraints for a new user"""
        logger.info(f"[DB] create_default_constraints: user_id={user_id}")
        default_constraints = [
            {
                "user_id": user_id,
                "name": "No study on night shifts",
                "description": "Study is not allowed during night shift days",
                "is_active": True,
                "rule": {"type": "no_activity_on", "activity": "study", "work_types": ["work_night"]},
                "is_system": True
            },
            {
                "user_id": user_id,
                "name": "Maximum 2 concurrent education commitments",
                "description": "Cannot have more than 2 active education commitments at once",
                "is_active": True,
                "rule": {"type": "max_concurrent", "scope": "education", "value": 2},
                "is_system": True
            },
            {
                "user_id": user_id,
                "name": "Work is immutable",
                "description": "Work schedule cannot be modified or removed by proposals",
                "is_active": True,
                "rule": {"type": "immutable", "scope": "work"},
                "is_system": True
            }
        ]
        try:
            result = self.client.table("constraints").insert(default_constraints).execute()
            logger.info(f"[DB] Created {len(result.data or [])} default constraints")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error creating default constraints: {e}")
            return []

    async def update_constraint(self, constraint_id: str, data: dict) -> dict:
        """Update a constraint"""
        logger.info(f"[DB] update_constraint: {constraint_id}")
        try:
            result = self.client.table("constraints").update(data).eq("id", constraint_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating constraint: {e}")
            return None

    async def delete_constraint(self, constraint_id: str) -> bool:
        """Delete a constraint"""
        logger.info(f"[DB] delete_constraint: {constraint_id}")
        try:
            self.client.table("constraints").delete().eq("id", constraint_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting constraint: {e}")
            return False
    
    # ==========================================
    # Commitments
    # ==========================================

    async def get_commitments(self, user_id: str) -> list:
        """Get all commitments for a user"""
        logger.debug(f"[DB] get_commitments: user_id={user_id}")
        try:
            result = self.client.table("commitments").select("*").eq("user_id", user_id).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} commitments")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting commitments: {e}")
            return []

    async def get_active_commitments(self, user_id: str) -> list:
        """Get only active commitments for a user"""
        logger.debug(f"[DB] get_active_commitments: user_id={user_id}")
        try:
            result = self.client.table("commitments").select("*").eq("user_id", user_id).eq("status", "active").execute()
            logger.debug(f"[DB] Found {len(result.data or [])} active commitments")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting active commitments: {e}")
            return []

    async def get_commitment(self, commitment_id: str) -> Optional[dict]:
        """Get a specific commitment"""
        logger.debug(f"[DB] get_commitment: {commitment_id}")
        try:
            result = self.client.table("commitments").select("*").eq("id", commitment_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting commitment: {e}")
            return None

    async def create_commitment(self, data: dict) -> dict:
        """Create a new commitment"""
        logger.info(f"[DB] create_commitment: name={data.get('name')}, type={data.get('type')}")
        try:
            result = self.client.table("commitments").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Commitment created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating commitment: {e}")
            return None

    async def update_commitment(self, commitment_id: str, data: dict) -> dict:
        """Update a commitment"""
        logger.info(f"[DB] update_commitment: {commitment_id} - fields: {list(data.keys())}")
        try:
            result = self.client.table("commitments").update(data).eq("id", commitment_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating commitment: {e}")
            return None

    async def delete_commitment(self, commitment_id: str) -> bool:
        """Delete a commitment"""
        logger.info(f"[DB] delete_commitment: {commitment_id}")
        try:
            self.client.table("commitments").delete().eq("id", commitment_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting commitment: {e}")
            return False

    # ==========================================
    # Leave Blocks
    # ==========================================

    async def get_leave_blocks(self, user_id: str) -> list:
        """Get all leave blocks for a user"""
        logger.debug(f"[DB] get_leave_blocks: user_id={user_id}")
        try:
            result = self.client.table("leave_blocks").select("*").eq("user_id", user_id).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} leave blocks")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting leave blocks: {e}")
            return []

    async def create_leave_block(self, data: dict) -> dict:
        """Create a new leave block"""
        logger.info(f"[DB] create_leave_block: {data.get('start_date')} to {data.get('end_date')}")
        try:
            result = self.client.table("leave_blocks").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Leave block created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating leave block: {e}")
            return None

    async def update_leave_block(self, leave_id: str, data: dict) -> dict:
        """Update a leave block"""
        logger.info(f"[DB] update_leave_block: {leave_id}")
        try:
            result = self.client.table("leave_blocks").update(data).eq("id", leave_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating leave block: {e}")
            return None

    async def delete_leave_block(self, leave_id: str) -> bool:
        """Delete a leave block"""
        logger.info(f"[DB] delete_leave_block: {leave_id}")
        try:
            self.client.table("leave_blocks").delete().eq("id", leave_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting leave block: {e}")
            return False

    # ==========================================
    # Calendar Days
    # ==========================================

    async def get_calendar_days(self, user_id: str, start_date: str, end_date: str) -> list:
        """Get calendar days for a date range"""
        logger.debug(f"[DB] get_calendar_days: user_id={user_id}, {start_date} to {end_date}")
        try:
            result = self.client.table("calendar_days").select("*").eq("user_id", user_id).gte("date", start_date).lte("date", end_date).order("date").execute()
            logger.debug(f"[DB] Found {len(result.data or [])} calendar days")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting calendar days: {e}")
            return []

    async def get_calendar_day(self, user_id: str, date: str) -> Optional[dict]:
        """Get a specific calendar day"""
        logger.debug(f"[DB] get_calendar_day: user_id={user_id}, date={date}")
        try:
            result = self.client.table("calendar_days").select("*").eq("user_id", user_id).eq("date", date).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting calendar day: {e}")
            return None

    async def upsert_calendar_days(self, days: list) -> list:
        """Insert or update multiple calendar days"""
        logger.info(f"[DB] upsert_calendar_days: {len(days)} days")
        try:
            result = self.client.table("calendar_days").upsert(days, on_conflict="user_id,date").execute()
            logger.debug(f"[DB] Upserted {len(result.data or [])} calendar days")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error upserting calendar days: {e}")
            return []

    async def delete_calendar_days(self, user_id: str, start_date: str, end_date: str) -> bool:
        """Delete calendar days in a date range"""
        logger.info(f"[DB] delete_calendar_days: user_id={user_id}, {start_date} to {end_date}")
        try:
            self.client.table("calendar_days").delete().eq("user_id", user_id).gte("date", start_date).lte("date", end_date).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting calendar days: {e}")
            return False

    async def get_all_calendar_years(self, user_id: str) -> list:
        """Get all calendar days for a user (to check which years exist)"""
        logger.debug(f"[DB] get_all_calendar_years: user_id={user_id}")
        try:
            result = self.client.table("calendar_days").select("date").eq("user_id", user_id).limit(1000).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting calendar years: {e}")
            return []
    
    # ==========================================
    # Mutations Log
    # ==========================================

    async def get_mutations(self, user_id: str, status: str = None, limit: int = 50) -> list:
        """Get mutations for a user"""
        logger.debug(f"[DB] get_mutations: user_id={user_id}, status={status}, limit={limit}")
        try:
            query = self.client.table("mutations_log").select("*").eq("user_id", user_id)
            if status:
                query = query.eq("status", status)
            result = query.order("proposed_at", desc=True).limit(limit).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} mutations")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting mutations: {e}")
            return []

    async def get_pending_mutations(self, user_id: str) -> list:
        """Get pending mutations for a user"""
        logger.debug(f"[DB] get_pending_mutations: user_id={user_id}")
        return await self.get_mutations(user_id, status="proposed")

    async def get_mutation(self, mutation_id: str) -> Optional[dict]:
        """Get a specific mutation"""
        logger.debug(f"[DB] get_mutation: {mutation_id}")
        try:
            result = self.client.table("mutations_log").select("*").eq("id", mutation_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting mutation: {e}")
            return None

    async def create_mutation(self, data: dict) -> dict:
        """Create a new mutation"""
        logger.info(f"[DB] create_mutation: type={data.get('type')}")
        try:
            result = self.client.table("mutations_log").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Mutation created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating mutation: {e}")
            return None

    async def update_mutation(self, mutation_id: str, data: dict) -> dict:
        """Update a mutation"""
        logger.info(f"[DB] update_mutation: {mutation_id} - fields: {list(data.keys())}")
        try:
            result = self.client.table("mutations_log").update(data).eq("id", mutation_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating mutation: {e}")
            return None

    # ==========================================
    # Snapshots
    # ==========================================

    async def create_snapshot(self, data: dict) -> dict:
        """Create a calendar snapshot"""
        logger.info(f"[DB] create_snapshot: user_id={data.get('user_id')}")
        try:
            result = self.client.table("calendar_snapshots").insert(data).execute()
            if result.data:
                logger.debug(f"[DB] Snapshot created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating snapshot: {e}")
            return None

    async def get_snapshots(self, user_id: str, limit: int = 10) -> list:
        """Get recent snapshots for a user"""
        logger.debug(f"[DB] get_snapshots: user_id={user_id}, limit={limit}")
        try:
            result = self.client.table("calendar_snapshots").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting snapshots: {e}")
            return []

    async def get_snapshot_by_hash(self, state_hash: str) -> Optional[dict]:
        """Get a snapshot by its hash"""
        logger.debug(f"[DB] get_snapshot_by_hash: {state_hash[:16]}...")
        try:
            result = self.client.table("calendar_snapshots").select("*").eq("state_hash", state_hash).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting snapshot by hash: {e}")
            return None

    # ==========================================
    # Subscriptions
    # ==========================================

    async def get_subscription(self, user_id: str) -> Optional[dict]:
        """Get user's subscription"""
        logger.debug(f"[DB] get_subscription: user_id={user_id}")
        try:
            result = self.client.table("subscriptions").select("*").eq("user_id", user_id).single().execute()
            if result.data:
                logger.debug(f"[DB] Subscription found: status={result.data.get('status')}")
            return result.data if result.data else None
        except Exception as e:
            logger.debug(f"[DB] No subscription found for user {user_id}")
            return None

    async def create_subscription(self, data: dict) -> dict:
        """Create a subscription"""
        logger.info(f"[DB] create_subscription: user_id={data.get('user_id')}, plan={data.get('plan')}")
        try:
            result = self.client.table("subscriptions").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Subscription created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating subscription: {e}")
            return None

    async def update_subscription(self, subscription_id: str, data: dict) -> dict:
        """Update a subscription"""
        logger.info(f"[DB] update_subscription: {subscription_id} - fields: {list(data.keys())}")
        try:
            result = self.client.table("subscriptions").update(data).eq("id", subscription_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating subscription: {e}")
            return None

    # ==========================================
    # Account Deletion
    # ==========================================

    async def delete_all_user_data(self, user_id: str) -> dict:
        """
        Delete all data for a user across all tables.
        Returns a summary of what was deleted.
        """
        logger.warning(f"[DB] === DELETING ALL USER DATA: user_id={user_id} ===")
        deleted = {}

        try:
            # Delete in order (respecting foreign key constraints)
            # 1. Calendar snapshots
            result = self.client.table("calendar_snapshots").delete().eq("user_id", user_id).execute()
            deleted["calendar_snapshots"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['calendar_snapshots']} calendar_snapshots")

            # 2. Mutations log
            result = self.client.table("mutations_log").delete().eq("user_id", user_id).execute()
            deleted["mutations_log"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['mutations_log']} mutations_log")

            # 3. Calendar days
            result = self.client.table("calendar_days").delete().eq("user_id", user_id).execute()
            deleted["calendar_days"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['calendar_days']} calendar_days")

            # 4. Leave blocks
            result = self.client.table("leave_blocks").delete().eq("user_id", user_id).execute()
            deleted["leave_blocks"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['leave_blocks']} leave_blocks")

            # 5. Commitments
            result = self.client.table("commitments").delete().eq("user_id", user_id).execute()
            deleted["commitments"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['commitments']} commitments")

            # 6. Constraints
            result = self.client.table("constraints").delete().eq("user_id", user_id).execute()
            deleted["constraints"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['constraints']} constraints")

            # 7. Cycles
            result = self.client.table("cycles").delete().eq("user_id", user_id).execute()
            deleted["cycles"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['cycles']} cycles")

            # 8. Subscriptions
            result = self.client.table("subscriptions").delete().eq("user_id", user_id).execute()
            deleted["subscriptions"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['subscriptions']} subscriptions")

            # 9. Users table (public.users)
            result = self.client.table("users").delete().eq("id", user_id).execute()
            deleted["users"] = len(result.data) if result.data else 0
            logger.info(f"[DB] Deleted {deleted['users']} users")

            logger.warning(f"[DB] === USER DATA DELETION COMPLETE: {deleted} ===")
        except Exception as e:
            logger.error(f"[DB] Error during user data deletion: {e}")

        return deleted

    async def delete_auth_user(self, auth_id: str) -> bool:
        """
        Delete user from Supabase Auth (auth.users).
        Requires admin client with service role key.
        """
        logger.warning(f"[DB] Deleting auth user: {auth_id}")
        try:
            admin_client = get_supabase_admin()
            admin_client.auth.admin.delete_user(auth_id)
            logger.info(f"[DB] Auth user deleted: {auth_id}")
            return True
        except Exception as e:
            logger.error(f"[DB] Failed to delete auth user {auth_id}: {e}")
            return False

    # ==========================================
    # Daily Logs
    # ==========================================

    async def get_daily_logs(self, user_id: str, start_date: str = None, end_date: str = None) -> list:
        """Get daily logs for a user, optionally filtered by date range"""
        logger.debug(f"[DB] get_daily_logs: user_id={user_id}, {start_date} to {end_date}")
        try:
            query = self.client.table("daily_logs").select("*").eq("user_id", user_id)
            if start_date:
                query = query.gte("date", start_date)
            if end_date:
                query = query.lte("date", end_date)
            result = query.order("date", desc=True).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} daily logs")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting daily logs: {e}")
            return []

    async def get_daily_log_by_date(self, user_id: str, date: str) -> Optional[dict]:
        """Get daily log for a specific date"""
        logger.debug(f"[DB] get_daily_log_by_date: user_id={user_id}, date={date}")
        try:
            result = self.client.table("daily_logs").select("*").eq("user_id", user_id).eq("date", date).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting daily log by date: {e}")
            return None

    async def get_daily_log(self, log_id: str) -> Optional[dict]:
        """Get a specific daily log by ID"""
        logger.debug(f"[DB] get_daily_log: {log_id}")
        try:
            result = self.client.table("daily_logs").select("*").eq("id", log_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting daily log: {e}")
            return None

    async def create_daily_log(self, data: dict) -> dict:
        """Create a new daily log"""
        logger.info(f"[DB] create_daily_log: date={data.get('date')}")
        try:
            result = self.client.table("daily_logs").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Daily log created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating daily log: {e}")
            return None

    async def update_daily_log(self, log_id: str, data: dict) -> dict:
        """Update a daily log"""
        logger.info(f"[DB] update_daily_log: {log_id}")
        try:
            result = self.client.table("daily_logs").update(data).eq("id", log_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating daily log: {e}")
            return None

    async def update_daily_hours(self, user_id: str, date: str, data: dict) -> dict:
        """Update or create hours for a specific date"""
        logger.info(f"[DB] update_daily_hours: user_id={user_id}, date={date}")
        try:
            # Check if log exists for this date
            existing = await self.get_daily_log_by_date(user_id, date)
            if existing:
                result = self.client.table("daily_logs").update(data).eq("id", existing["id"]).execute()
            else:
                # Create a new log entry with just hours
                new_data = {
                    "user_id": user_id,
                    "date": date,
                    "note": "",
                    **data
                }
                result = self.client.table("daily_logs").insert(new_data).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating daily hours: {e}")
            return None

    async def delete_daily_log(self, log_id: str) -> bool:
        """Delete a daily log"""
        logger.info(f"[DB] delete_daily_log: {log_id}")
        try:
            self.client.table("daily_logs").delete().eq("id", log_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting daily log: {e}")
            return False

    # ==========================================
    # Incidents
    # ==========================================

    async def get_incidents(self, user_id: str, start_date: str = None, end_date: str = None) -> list:
        """Get incidents for a user, optionally filtered by date range"""
        logger.debug(f"[DB] get_incidents: user_id={user_id}, {start_date} to {end_date}")
        try:
            query = self.client.table("incidents").select("*").eq("user_id", user_id)
            if start_date:
                query = query.gte("date", start_date)
            if end_date:
                query = query.lte("date", end_date)
            result = query.order("date", desc=True).execute()
            logger.debug(f"[DB] Found {len(result.data or [])} incidents")
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting incidents: {e}")
            return []

    async def get_incidents_by_date(self, user_id: str, date: str) -> list:
        """Get all incidents for a specific date"""
        logger.debug(f"[DB] get_incidents_by_date: user_id={user_id}, date={date}")
        try:
            result = self.client.table("incidents").select("*").eq("user_id", user_id).eq("date", date).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting incidents by date: {e}")
            return []

    async def get_incident(self, incident_id: str) -> Optional[dict]:
        """Get a specific incident by ID"""
        logger.debug(f"[DB] get_incident: {incident_id}")
        try:
            result = self.client.table("incidents").select("*").eq("id", incident_id).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting incident: {e}")
            return None

    async def create_incident(self, data: dict) -> dict:
        """Create a new incident"""
        logger.info(f"[DB] create_incident: date={data.get('date')}, type={data.get('type')}")
        try:
            result = self.client.table("incidents").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Incident created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating incident: {e}")
            return None

    async def update_incident(self, incident_id: str, data: dict) -> dict:
        """Update an incident"""
        logger.info(f"[DB] update_incident: {incident_id}")
        try:
            result = self.client.table("incidents").update(data).eq("id", incident_id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error updating incident: {e}")
            return None

    async def delete_incident(self, incident_id: str) -> bool:
        """Delete an incident"""
        logger.info(f"[DB] delete_incident: {incident_id}")
        try:
            self.client.table("incidents").delete().eq("id", incident_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error deleting incident: {e}")
            return False

    async def get_incident_stats(self, user_id: str, year: int = None) -> dict:
        """Get incident statistics for a user"""
        logger.debug(f"[DB] get_incident_stats: user_id={user_id}, year={year}")
        try:
            query = self.client.table("incidents").select("*").eq("user_id", user_id)
            if year:
                query = query.gte("date", f"{year}-01-01").lte("date", f"{year}-12-31")
            result = query.execute()

            incidents = result.data or []
            stats = {
                "total_count": len(incidents),
                "by_type": {},
                "by_severity": {},
                "by_month": {}
            }

            for incident in incidents:
                # Count by type
                itype = incident.get("type", "other")
                stats["by_type"][itype] = stats["by_type"].get(itype, 0) + 1

                # Count by severity
                severity = incident.get("severity", "medium")
                stats["by_severity"][severity] = stats["by_severity"].get(severity, 0) + 1

                # Count by month
                date_str = incident.get("date", "")
                if date_str and len(date_str) >= 7:
                    month = date_str[:7]  # "2026-01"
                    stats["by_month"][month] = stats["by_month"].get(month, 0) + 1

            return stats
        except Exception as e:
            logger.error(f"[DB] Error getting incident stats: {e}")
            return {"total_count": 0, "by_type": {}, "by_severity": {}, "by_month": {}}

    # ==================== CALENDAR SHARING ====================

    async def create_calendar_share(self, data: dict) -> dict:
        """Create a new calendar share link"""
        logger.info(f"[DB] create_calendar_share: user_id={data.get('user_id')}")
        try:
            result = self.client.table("calendar_shares").insert(data).execute()
            if result.data:
                logger.info(f"[DB] Calendar share created: {result.data[0].get('id')}")
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error creating calendar share: {e}")
            return None

    async def get_calendar_shares(self, user_id: str) -> list:
        """Get all calendar shares for a user"""
        logger.debug(f"[DB] get_calendar_shares: user_id={user_id}")
        try:
            result = self.client.table("calendar_shares").select("*").eq(
                "user_id", user_id
            ).order("created_at", desc=True).execute()
            return result.data or []
        except Exception as e:
            logger.error(f"[DB] Error getting calendar shares: {e}")
            return []

    async def get_calendar_share_by_code(self, share_code: str) -> Optional[dict]:
        """Get a calendar share by its unique code"""
        logger.debug(f"[DB] get_calendar_share_by_code: {share_code}")
        try:
            result = self.client.table("calendar_shares").select("*").eq(
                "share_code", share_code
            ).eq("is_active", True).single().execute()
            return result.data if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting calendar share by code: {e}")
            return None

    async def revoke_calendar_share(self, share_id: str, user_id: str) -> bool:
        """Revoke a calendar share (set is_active to False)"""
        logger.info(f"[DB] revoke_calendar_share: {share_id}")
        try:
            self.client.table("calendar_shares").update({
                "is_active": False
            }).eq("id", share_id).eq("user_id", user_id).execute()
            return True
        except Exception as e:
            logger.error(f"[DB] Error revoking calendar share: {e}")
            return False

    async def get_user_name_by_id(self, user_id: str) -> Optional[str]:
        """Get a user's display name by their ID"""
        try:
            result = self.client.table("users").select("name").eq("id", user_id).single().execute()
            return result.data.get("name") if result.data else None
        except Exception as e:
            logger.error(f"[DB] Error getting user name: {e}")
            return None

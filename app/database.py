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
    
    # ==========================================
    # Users
    # ==========================================
    
    async def get_user_by_auth_id(self, auth_id: str) -> Optional[dict]:
        """Get user by Supabase auth ID"""
        result = self.client.table("users").select("*").eq("auth_id", auth_id).single().execute()
        return result.data if result.data else None
    
    async def create_user(self, data: dict) -> Optional[dict]:
        """Create a new user"""
        result = self.client.table("users").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def get_user_by_id(self, user_id: str) -> Optional[dict]:
        """Get user by internal ID"""
        result = self.client.table("users").select("*").eq("id", user_id).single().execute()
        return result.data if result.data else None
    
    async def update_user(self, user_id: str, data: dict) -> dict:
        """Update user data"""
        result = self.client.table("users").update(data).eq("id", user_id).execute()
        return result.data[0] if result.data else None
    
    async def complete_onboarding(self, user_id: str) -> dict:
        """Mark user onboarding as completed"""
        return await self.update_user(user_id, {"onboarding_completed": True})
    
    # ==========================================
    # Cycles
    # ==========================================
    
    async def get_cycles(self, user_id: str) -> list:
        """Get all cycles for a user"""
        result = self.client.table("cycles").select("*").eq("user_id", user_id).execute()
        return result.data or []
    
    async def get_active_cycle(self, user_id: str) -> Optional[dict]:
        """Get the active cycle for a user"""
        result = self.client.table("cycles").select("*").eq("user_id", user_id).eq("is_active", True).single().execute()
        return result.data if result.data else None
    
    async def create_cycle(self, data: dict) -> dict:
        """Create a new cycle"""
        result = self.client.table("cycles").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def update_cycle(self, cycle_id: str, data: dict) -> dict:
        """Update a cycle"""
        result = self.client.table("cycles").update(data).eq("id", cycle_id).execute()
        return result.data[0] if result.data else None
    
    async def delete_cycle(self, cycle_id: str) -> bool:
        """Delete a cycle"""
        self.client.table("cycles").delete().eq("id", cycle_id).execute()
        return True
    
    # ==========================================
    # Constraints
    # ==========================================
    
    async def get_constraints(self, user_id: str) -> list:
        """Get all constraints for a user"""
        result = self.client.table("constraints").select("*").eq("user_id", user_id).execute()
        return result.data or []
    
    async def get_active_constraints(self, user_id: str) -> list:
        """Get only active constraints for a user"""
        result = self.client.table("constraints").select("*").eq("user_id", user_id).eq("is_active", True).execute()
        return result.data or []
    
    async def create_constraint(self, data: dict) -> dict:
        """Create a new constraint"""
        result = self.client.table("constraints").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def create_default_constraints(self, user_id: str) -> list:
        """Create default system constraints for a new user"""
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
        result = self.client.table("constraints").insert(default_constraints).execute()
        return result.data or []
    
    async def update_constraint(self, constraint_id: str, data: dict) -> dict:
        """Update a constraint"""
        result = self.client.table("constraints").update(data).eq("id", constraint_id).execute()
        return result.data[0] if result.data else None
    
    async def delete_constraint(self, constraint_id: str) -> bool:
        """Delete a constraint"""
        self.client.table("constraints").delete().eq("id", constraint_id).execute()
        return True
    
    # ==========================================
    # Commitments
    # ==========================================
    
    async def get_commitments(self, user_id: str) -> list:
        """Get all commitments for a user"""
        result = self.client.table("commitments").select("*").eq("user_id", user_id).execute()
        return result.data or []
    
    async def get_active_commitments(self, user_id: str) -> list:
        """Get only active commitments for a user"""
        result = self.client.table("commitments").select("*").eq("user_id", user_id).eq("status", "active").execute()
        return result.data or []
    
    async def get_commitment(self, commitment_id: str) -> Optional[dict]:
        """Get a specific commitment"""
        result = self.client.table("commitments").select("*").eq("id", commitment_id).single().execute()
        return result.data if result.data else None
    
    async def create_commitment(self, data: dict) -> dict:
        """Create a new commitment"""
        result = self.client.table("commitments").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def update_commitment(self, commitment_id: str, data: dict) -> dict:
        """Update a commitment"""
        result = self.client.table("commitments").update(data).eq("id", commitment_id).execute()
        return result.data[0] if result.data else None
    
    async def delete_commitment(self, commitment_id: str) -> bool:
        """Delete a commitment"""
        self.client.table("commitments").delete().eq("id", commitment_id).execute()
        return True
    
    # ==========================================
    # Leave Blocks
    # ==========================================
    
    async def get_leave_blocks(self, user_id: str) -> list:
        """Get all leave blocks for a user"""
        result = self.client.table("leave_blocks").select("*").eq("user_id", user_id).execute()
        return result.data or []
    
    async def create_leave_block(self, data: dict) -> dict:
        """Create a new leave block"""
        result = self.client.table("leave_blocks").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def update_leave_block(self, leave_id: str, data: dict) -> dict:
        """Update a leave block"""
        result = self.client.table("leave_blocks").update(data).eq("id", leave_id).execute()
        return result.data[0] if result.data else None
    
    async def delete_leave_block(self, leave_id: str) -> bool:
        """Delete a leave block"""
        self.client.table("leave_blocks").delete().eq("id", leave_id).execute()
        return True
    
    # ==========================================
    # Calendar Days
    # ==========================================
    
    async def get_calendar_days(self, user_id: str, start_date: str, end_date: str) -> list:
        """Get calendar days for a date range"""
        result = self.client.table("calendar_days").select("*").eq("user_id", user_id).gte("date", start_date).lte("date", end_date).order("date").execute()
        return result.data or []
    
    async def get_calendar_day(self, user_id: str, date: str) -> Optional[dict]:
        """Get a specific calendar day"""
        result = self.client.table("calendar_days").select("*").eq("user_id", user_id).eq("date", date).single().execute()
        return result.data if result.data else None
    
    async def upsert_calendar_days(self, days: list) -> list:
        """Insert or update multiple calendar days"""
        result = self.client.table("calendar_days").upsert(days, on_conflict="user_id,date").execute()
        return result.data or []
    
    async def delete_calendar_days(self, user_id: str, start_date: str, end_date: str) -> bool:
        """Delete calendar days in a date range"""
        self.client.table("calendar_days").delete().eq("user_id", user_id).gte("date", start_date).lte("date", end_date).execute()
        return True
    
    async def get_all_calendar_years(self, user_id: str) -> list:
        """Get all calendar days for a user (to check which years exist)"""
        result = self.client.table("calendar_days").select("date").eq("user_id", user_id).limit(1000).execute()
        return result.data or []
    
    # ==========================================
    # Mutations Log
    # ==========================================
    
    async def get_mutations(self, user_id: str, status: str = None, limit: int = 50) -> list:
        """Get mutations for a user"""
        query = self.client.table("mutations_log").select("*").eq("user_id", user_id)
        if status:
            query = query.eq("status", status)
        result = query.order("proposed_at", desc=True).limit(limit).execute()
        return result.data or []
    
    async def get_pending_mutations(self, user_id: str) -> list:
        """Get pending mutations for a user"""
        return await self.get_mutations(user_id, status="proposed")
    
    async def get_mutation(self, mutation_id: str) -> Optional[dict]:
        """Get a specific mutation"""
        result = self.client.table("mutations_log").select("*").eq("id", mutation_id).single().execute()
        return result.data if result.data else None
    
    async def create_mutation(self, data: dict) -> dict:
        """Create a new mutation"""
        result = self.client.table("mutations_log").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def update_mutation(self, mutation_id: str, data: dict) -> dict:
        """Update a mutation"""
        result = self.client.table("mutations_log").update(data).eq("id", mutation_id).execute()
        return result.data[0] if result.data else None
    
    # ==========================================
    # Snapshots
    # ==========================================
    
    async def create_snapshot(self, data: dict) -> dict:
        """Create a calendar snapshot"""
        result = self.client.table("calendar_snapshots").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def get_snapshots(self, user_id: str, limit: int = 10) -> list:
        """Get recent snapshots for a user"""
        result = self.client.table("calendar_snapshots").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(limit).execute()
        return result.data or []
    
    async def get_snapshot_by_hash(self, state_hash: str) -> Optional[dict]:
        """Get a snapshot by its hash"""
        result = self.client.table("calendar_snapshots").select("*").eq("state_hash", state_hash).single().execute()
        return result.data if result.data else None
    
    # ==========================================
    # Subscriptions
    # ==========================================
    
    async def get_subscription(self, user_id: str) -> Optional[dict]:
        """Get user's subscription"""
        result = self.client.table("subscriptions").select("*").eq("user_id", user_id).single().execute()
        return result.data if result.data else None
    
    async def create_subscription(self, data: dict) -> dict:
        """Create a subscription"""
        result = self.client.table("subscriptions").insert(data).execute()
        return result.data[0] if result.data else None
    
    async def update_subscription(self, subscription_id: str, data: dict) -> dict:
        """Update a subscription"""
        result = self.client.table("subscriptions").update(data).eq("id", subscription_id).execute()
        return result.data[0] if result.data else None
    
    # ==========================================
    # Account Deletion
    # ==========================================
    
    async def delete_all_user_data(self, user_id: str) -> dict:
        """
        Delete all data for a user across all tables.
        Returns a summary of what was deleted.
        """
        deleted = {}
        
        # Delete in order (respecting foreign key constraints)
        # 1. Calendar snapshots
        result = self.client.table("calendar_snapshots").delete().eq("user_id", user_id).execute()
        deleted["calendar_snapshots"] = len(result.data) if result.data else 0
        
        # 2. Mutations log
        result = self.client.table("mutations_log").delete().eq("user_id", user_id).execute()
        deleted["mutations_log"] = len(result.data) if result.data else 0
        
        # 3. Calendar days
        result = self.client.table("calendar_days").delete().eq("user_id", user_id).execute()
        deleted["calendar_days"] = len(result.data) if result.data else 0
        
        # 4. Leave blocks
        result = self.client.table("leave_blocks").delete().eq("user_id", user_id).execute()
        deleted["leave_blocks"] = len(result.data) if result.data else 0
        
        # 5. Commitments
        result = self.client.table("commitments").delete().eq("user_id", user_id).execute()
        deleted["commitments"] = len(result.data) if result.data else 0
        
        # 6. Constraints
        result = self.client.table("constraints").delete().eq("user_id", user_id).execute()
        deleted["constraints"] = len(result.data) if result.data else 0
        
        # 7. Cycles
        result = self.client.table("cycles").delete().eq("user_id", user_id).execute()
        deleted["cycles"] = len(result.data) if result.data else 0
        
        # 8. Subscriptions
        result = self.client.table("subscriptions").delete().eq("user_id", user_id).execute()
        deleted["subscriptions"] = len(result.data) if result.data else 0
        
        # 9. Users table (public.users)
        result = self.client.table("users").delete().eq("id", user_id).execute()
        deleted["users"] = len(result.data) if result.data else 0
        
        return deleted
    
    async def delete_auth_user(self, auth_id: str) -> bool:
        """
        Delete user from Supabase Auth (auth.users).
        Requires admin client with service role key.
        """
        try:
            admin_client = get_supabase_admin()
            admin_client.auth.admin.delete_user(auth_id)
            return True
        except Exception as e:
            logger.error(f"Failed to delete auth user {auth_id}: {e}")
            return False

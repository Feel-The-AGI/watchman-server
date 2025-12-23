"""
Master Settings Service
Handles all operations on the user's master settings document
"""

from typing import Dict, Any, Optional
from datetime import datetime
from loguru import logger
import json

from app.database import Database


# Default master settings template
DEFAULT_MASTER_SETTINGS = {
    "cycle": None,
    "work": {
        "shift_hours": 12,
        "shift_start": "06:00",
        "shift_end": "18:00",
        "break_minutes": 60
    },
    "constraints": [],
    "commitments": [],
    "leave_blocks": [],
    "preferences": {
        "timezone": "UTC",
        "week_starts_on": "monday",
        "theme": "dark",
        "notifications": True
    }
}


class MasterSettingsService:
    """Service for managing master settings"""
    
    def __init__(self, db: Database):
        self.db = db
    
    async def get(self, user_id: str) -> Dict[str, Any]:
        """
        Get user's master settings, creating default if not exists
        
        Args:
            user_id: The user's ID
            
        Returns:
            The master settings document
        """
        result = self.db.client.table("master_settings").select("*").eq("user_id", user_id).execute()
        
        if result.data and len(result.data) > 0:
            row = result.data[0]
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "settings": row["settings"],
                "version": row["version"],
                "updated_at": row["updated_at"]
            }
        
        # Create default settings
        return await self.create_default(user_id)
    
    async def create_default(self, user_id: str) -> Dict[str, Any]:
        """
        Create default master settings for a new user
        
        Args:
            user_id: The user's ID
            
        Returns:
            The created master settings document
        """
        data = {
            "user_id": user_id,
            "settings": DEFAULT_MASTER_SETTINGS,
            "version": 1
        }
        
        result = self.db.client.table("master_settings").insert(data).execute()
        
        if result.data and len(result.data) > 0:
            row = result.data[0]
            logger.info(f"Created default master settings for user {user_id}")
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "settings": row["settings"],
                "version": row["version"],
                "updated_at": row["updated_at"]
            }
        
        raise Exception("Failed to create master settings")
    
    async def update(
        self, 
        user_id: str, 
        settings: Dict[str, Any],
        expected_version: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Update user's master settings
        
        Args:
            user_id: The user's ID
            settings: The new settings document
            expected_version: For optimistic locking (optional)
            
        Returns:
            The updated master settings document
        """
        # Get current version
        current = await self.get(user_id)
        
        if expected_version is not None and current["version"] != expected_version:
            raise ValueError(f"Version mismatch: expected {expected_version}, got {current['version']}")
        
        new_version = current["version"] + 1
        
        result = self.db.client.table("master_settings").update({
            "settings": settings,
            "version": new_version
        }).eq("user_id", user_id).execute()
        
        if result.data and len(result.data) > 0:
            row = result.data[0]
            logger.info(f"Updated master settings for user {user_id} to version {new_version}")
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "settings": row["settings"],
                "version": row["version"],
                "updated_at": row["updated_at"]
            }
        
        raise Exception("Failed to update master settings")
    
    async def update_section(
        self,
        user_id: str,
        section: str,
        value: Any
    ) -> Dict[str, Any]:
        """
        Update a specific section of master settings
        
        Args:
            user_id: The user's ID
            section: The section to update (e.g., 'cycle', 'work', 'constraints')
            value: The new value for the section
            
        Returns:
            The updated master settings document
        """
        current = await self.get(user_id)
        settings = current["settings"].copy()
        settings[section] = value
        
        return await self.update(user_id, settings, current["version"])
    
    async def add_to_list(
        self,
        user_id: str,
        section: str,
        item: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add an item to a list section (constraints, commitments, leave_blocks)
        
        Args:
            user_id: The user's ID
            section: The list section to add to
            item: The item to add
            
        Returns:
            The updated master settings document
        """
        current = await self.get(user_id)
        settings = current["settings"].copy()
        
        if section not in settings:
            settings[section] = []
        
        settings[section].append(item)
        
        return await self.update(user_id, settings, current["version"])
    
    async def remove_from_list(
        self,
        user_id: str,
        section: str,
        item_id: str
    ) -> Dict[str, Any]:
        """
        Remove an item from a list section by ID
        
        Args:
            user_id: The user's ID
            section: The list section
            item_id: The ID of the item to remove
            
        Returns:
            The updated master settings document
        """
        current = await self.get(user_id)
        settings = current["settings"].copy()
        
        if section in settings and isinstance(settings[section], list):
            settings[section] = [
                item for item in settings[section] 
                if item.get("id") != item_id
            ]
        
        return await self.update(user_id, settings, current["version"])
    
    async def get_snapshot(self, user_id: str) -> Dict[str, Any]:
        """
        Get a snapshot of current settings for command logging
        
        Args:
            user_id: The user's ID
            
        Returns:
            The settings snapshot
        """
        result = await self.get(user_id)
        return result["settings"]


def create_master_settings_service(db: Database) -> MasterSettingsService:
    """Factory function"""
    return MasterSettingsService(db)

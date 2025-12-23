"""
Command Executor Service
Validates commands, routes through Constraint Engine, creates proposals, executes approved commands
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, date
from uuid import uuid4
from loguru import logger

from app.database import Database
from app.engines.master_settings_service import MasterSettingsService
from app.engines.calendar_engine import create_calendar_engine


# Command action types
VALID_ACTIONS = {
    "update_cycle",
    "add_commitment", 
    "remove_commitment",
    "add_leave",
    "remove_leave",
    "update_constraint",
    "remove_constraint",
    "undo",
    "redo"
}


class CommandExecutor:
    """
    Executes commands from the agent.
    Guards all changes through the Constraint Engine.
    """
    
    def __init__(self, db: Database, user_id: str):
        self.db = db
        self.user_id = user_id
        self.settings_service = MasterSettingsService(db)
    
    async def validate_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate a command and check constraints.
        Does NOT execute - just validates.
        
        Args:
            command: The command to validate
            
        Returns:
            Validation result with {valid, violations, warnings, alternatives}
        """
        action = command.get("action")
        payload = command.get("payload", {})
        
        if action not in VALID_ACTIONS:
            return {
                "valid": False,
                "violations": [{"type": "invalid_action", "message": f"Unknown action: {action}"}],
                "warnings": [],
                "alternatives": []
            }
        
        # Get current settings for context
        current_settings = await self.settings_service.get_snapshot(self.user_id)
        
        # Run constraint validation
        violations, warnings = await self._check_constraints(action, payload, current_settings)
        
        # Generate alternatives if there are violations
        alternatives = []
        if violations:
            alternatives = await self._generate_alternatives(action, payload, current_settings, violations)
        
        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "warnings": warnings,
            "alternatives": alternatives
        }
    
    async def create_proposal(
        self, 
        command: Dict[str, Any],
        message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create a proposal for user approval.
        
        Args:
            command: The command to propose
            message_id: Optional chat message ID that triggered this
            
        Returns:
            The created proposal
        """
        # Validate first
        validation = await self.validate_command(command)
        
        # Create proposal record
        proposal_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "command": command,
            "validation": validation,
            "status": "pending",
            "message_id": message_id
        }
        
        result = self.db.client.table("proposals").insert(proposal_data).execute()
        
        if result.data and len(result.data) > 0:
            proposal = result.data[0]
            logger.info(f"Created proposal {proposal['id']} for user {self.user_id}")
            return proposal
        
        raise Exception("Failed to create proposal")
    
    async def execute(
        self, 
        command: Dict[str, Any],
        source: str = "chat",
        message_id: Optional[str] = None,
        skip_validation: bool = False
    ) -> Dict[str, Any]:
        """
        Execute a command and log it.
        
        Args:
            command: The command to execute
            source: Where this command came from ('chat', 'ui', 'api')
            message_id: Optional chat message ID
            skip_validation: Skip constraint validation (for undo/redo)
            
        Returns:
            Result of the execution
        """
        action = command.get("action")
        payload = command.get("payload", {})
        explanation = command.get("explanation", "")
        
        if action not in VALID_ACTIONS:
            raise ValueError(f"Unknown action: {action}")
        
        # Validate unless skipping
        if not skip_validation and action not in ["undo", "redo"]:
            validation = await self.validate_command(command)
            if not validation["valid"]:
                return {
                    "success": False,
                    "error": "Command has constraint violations",
                    "validation": validation
                }
        
        # Get before state
        before_state = await self.settings_service.get_snapshot(self.user_id)
        
        # Execute the action
        try:
            result = await self._execute_action(action, payload)
        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            return {
                "success": False,
                "error": str(e)
            }
        
        # Get after state
        after_state = await self.settings_service.get_snapshot(self.user_id)
        
        # Log the command
        command_log = await self._log_command(
            action=action,
            payload=payload,
            before_state=before_state,
            after_state=after_state,
            source=source,
            message_id=message_id,
            explanation=explanation
        )
        
        # Regenerate calendar
        await self._regenerate_calendar()
        
        return {
            "success": True,
            "command_id": command_log["id"],
            "result": result
        }
    
    async def _execute_action(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a specific action type"""
        
        if action == "update_cycle":
            return await self._action_update_cycle(payload)
        elif action == "add_commitment":
            return await self._action_add_commitment(payload)
        elif action == "remove_commitment":
            return await self._action_remove_commitment(payload)
        elif action == "add_leave":
            return await self._action_add_leave(payload)
        elif action == "remove_leave":
            return await self._action_remove_leave(payload)
        elif action == "update_constraint":
            return await self._action_update_constraint(payload)
        elif action == "remove_constraint":
            return await self._action_remove_constraint(payload)
        elif action == "undo":
            return await self._action_undo(payload)
        elif action == "redo":
            return await self._action_redo(payload)
        else:
            raise ValueError(f"Unknown action: {action}")
    
    async def _action_update_cycle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update the work cycle - can update full cycle or just anchor"""
        
        # Get existing cycle to preserve data not being updated
        current = await self.settings_service.get(self.user_id)
        existing_cycle = current.get("settings", {}).get("cycle", {}) or {}
        
        # If only anchor is provided (correction), preserve existing pattern
        if "anchor" in payload and "pattern" not in payload:
            # Anchor-only update
            cycle_data = {
                "id": existing_cycle.get("id", str(uuid4())),
                "name": existing_cycle.get("name", "My Rotation"),
                "pattern": existing_cycle.get("pattern", []),
                "anchor": payload.get("anchor", existing_cycle.get("anchor", {})),
                "total_days": existing_cycle.get("total_days", 15)
            }
        else:
            # Full cycle update
            cycle_data = {
                "id": payload.get("id", existing_cycle.get("id", str(uuid4()))),
                "name": payload.get("name", existing_cycle.get("name", "My Rotation")),
                "pattern": payload.get("pattern", existing_cycle.get("pattern", [])),
                "anchor": payload.get("anchor", existing_cycle.get("anchor", {})),
                "total_days": sum(block.get("days", 0) for block in payload.get("pattern", [])) or existing_cycle.get("total_days", 15)
            }
        
        await self.settings_service.update_section(self.user_id, "cycle", cycle_data)
        
        # Also update work settings if provided
        if "shift_hours" in payload:
            work = current.get("settings", {}).get("work", {})
            work["shift_hours"] = payload.get("shift_hours", work.get("shift_hours", 12))
            work["shift_start"] = payload.get("shift_start", work.get("shift_start", "06:00"))
            await self.settings_service.update_section(self.user_id, "work", work)
        
        return {"cycle": cycle_data}
    
    async def _action_add_commitment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new commitment"""
        commitment = {
            "id": payload.get("id", str(uuid4())),
            "name": payload.get("name"),
            "type": payload.get("type", "personal"),
            "schedule": payload.get("schedule", {}),
            "constraints": payload.get("constraints", {}),
            "status": "active",
            "created_at": datetime.utcnow().isoformat()
        }
        
        await self.settings_service.add_to_list(self.user_id, "commitments", commitment)
        return {"commitment": commitment}
    
    async def _action_remove_commitment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a commitment"""
        commitment_id = payload.get("id")
        if not commitment_id:
            raise ValueError("Commitment ID required")
        
        await self.settings_service.remove_from_list(self.user_id, "commitments", commitment_id)
        return {"removed_id": commitment_id}
    
    async def _action_add_leave(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add a leave block"""
        leave = {
            "id": payload.get("id", str(uuid4())),
            "name": payload.get("name", "Leave"),
            "type": payload.get("type", "annual"),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "notes": payload.get("notes", ""),
            "created_at": datetime.utcnow().isoformat()
        }
        
        await self.settings_service.add_to_list(self.user_id, "leave_blocks", leave)
        return {"leave": leave}
    
    async def _action_remove_leave(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a leave block"""
        leave_id = payload.get("id")
        if not leave_id:
            raise ValueError("Leave ID required")
        
        await self.settings_service.remove_from_list(self.user_id, "leave_blocks", leave_id)
        return {"removed_id": leave_id}
    
    async def _action_update_constraint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add or update a constraint"""
        constraint_id = payload.get("id", str(uuid4()))
        
        # Check if updating existing
        current = await self.settings_service.get(self.user_id)
        constraints = current["settings"].get("constraints", [])
        
        existing_idx = None
        for i, c in enumerate(constraints):
            if c.get("id") == constraint_id:
                existing_idx = i
                break
        
        constraint = {
            "id": constraint_id,
            "rule": payload.get("rule"),
            "type": payload.get("type", "soft"),  # hard, soft
            "description": payload.get("description", ""),
            "value": payload.get("value"),
            "active": payload.get("active", True),
            "created_at": datetime.utcnow().isoformat()
        }
        
        if existing_idx is not None:
            constraints[existing_idx] = constraint
            await self.settings_service.update_section(self.user_id, "constraints", constraints)
        else:
            await self.settings_service.add_to_list(self.user_id, "constraints", constraint)
        
        return {"constraint": constraint}
    
    async def _action_remove_constraint(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a constraint"""
        constraint_id = payload.get("id")
        if not constraint_id:
            raise ValueError("Constraint ID required")
        
        await self.settings_service.remove_from_list(self.user_id, "constraints", constraint_id)
        return {"removed_id": constraint_id}
    
    async def _action_undo(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Undo the last command"""
        # Find last applied command
        result = self.db.client.table("command_log").select("*").eq(
            "user_id", self.user_id
        ).eq("status", "applied").order("created_at", desc=True).limit(1).execute()
        
        if not result.data or len(result.data) == 0:
            return {"message": "Nothing to undo"}
        
        command = result.data[0]
        
        # Restore before state
        before_state = command.get("before_state")
        if before_state:
            await self.settings_service.update(self.user_id, before_state)
        
        # Mark command as undone
        self.db.client.table("command_log").update({
            "status": "undone"
        }).eq("id", command["id"]).execute()
        
        return {"undone_command_id": command["id"]}
    
    async def _action_redo(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Redo the last undone command"""
        # Find last undone command
        result = self.db.client.table("command_log").select("*").eq(
            "user_id", self.user_id
        ).eq("status", "undone").order("created_at", desc=True).limit(1).execute()
        
        if not result.data or len(result.data) == 0:
            return {"message": "Nothing to redo"}
        
        command = result.data[0]
        
        # Restore after state
        after_state = command.get("after_state")
        if after_state:
            await self.settings_service.update(self.user_id, after_state)
        
        # Mark command as redone
        self.db.client.table("command_log").update({
            "status": "redone"
        }).eq("id", command["id"]).execute()
        
        return {"redone_command_id": command["id"]}
    
    async def _check_constraints(
        self, 
        action: str, 
        payload: Dict[str, Any],
        current_settings: Dict[str, Any]
    ) -> Tuple[List[Dict], List[Dict]]:
        """Check constraints and return violations and warnings"""
        violations = []
        warnings = []
        
        constraints = current_settings.get("constraints", [])
        
        # For add_commitment, check against constraints
        if action == "add_commitment":
            schedule = payload.get("schedule", {})
            
            for constraint in constraints:
                if not constraint.get("active", True):
                    continue
                
                rule = constraint.get("rule")
                constraint_type = constraint.get("type", "soft")
                
                # Example: no_study_on_night_shift
                if rule == "no_study_on_night_shift" and payload.get("type") == "education":
                    # Would need to check actual calendar days - for now just warn
                    if constraint_type == "hard":
                        violations.append({
                            "constraint_id": constraint.get("id"),
                            "rule": rule,
                            "message": "Cannot schedule study on night shift days",
                            "type": "hard"
                        })
                    else:
                        warnings.append({
                            "constraint_id": constraint.get("id"),
                            "rule": rule,
                            "message": "This may conflict with night shifts",
                            "type": "soft"
                        })
        
        # For add_leave, check for overlaps
        if action == "add_leave":
            existing_leaves = current_settings.get("leave_blocks", [])
            new_start = payload.get("start_date")
            new_end = payload.get("end_date")
            
            for leave in existing_leaves:
                if self._dates_overlap(
                    new_start, new_end,
                    leave.get("start_date"), leave.get("end_date")
                ):
                    warnings.append({
                        "type": "overlap",
                        "message": f"Overlaps with existing leave: {leave.get('name')}",
                        "existing_leave_id": leave.get("id")
                    })
        
        return violations, warnings
    
    def _dates_overlap(self, start1: str, end1: str, start2: str, end2: str) -> bool:
        """Check if two date ranges overlap"""
        try:
            s1 = date.fromisoformat(start1) if start1 else None
            e1 = date.fromisoformat(end1) if end1 else None
            s2 = date.fromisoformat(start2) if start2 else None
            e2 = date.fromisoformat(end2) if end2 else None
            
            if not all([s1, e1, s2, e2]):
                return False
            
            return s1 <= e2 and s2 <= e1
        except:
            return False
    
    async def _generate_alternatives(
        self,
        action: str,
        payload: Dict[str, Any],
        current_settings: Dict[str, Any],
        violations: List[Dict]
    ) -> List[Dict]:
        """Generate alternative commands when violations exist"""
        alternatives = []
        
        # For now, suggest skipping the problematic constraint
        for violation in violations:
            if violation.get("type") == "hard":
                alternatives.append({
                    "id": str(uuid4()),
                    "description": f"Proceed anyway (override {violation.get('rule')})",
                    "modified_payload": {**payload, "_override_constraint": violation.get("constraint_id")}
                })
        
        return alternatives
    
    async def _log_command(
        self,
        action: str,
        payload: Dict[str, Any],
        before_state: Dict[str, Any],
        after_state: Dict[str, Any],
        source: str,
        message_id: Optional[str],
        explanation: str
    ) -> Dict[str, Any]:
        """Log a command to the command_log table"""
        log_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "action": action,
            "payload": payload,
            "before_state": before_state,
            "after_state": after_state,
            "status": "applied",
            "source": source,
            "message_id": message_id,
            "explanation": explanation
        }
        
        result = self.db.client.table("command_log").insert(log_data).execute()
        
        if result.data and len(result.data) > 0:
            return result.data[0]
        
        raise Exception("Failed to log command")
    
    async def _regenerate_calendar(self):
        """Regenerate calendar days from current settings"""
        settings = await self.settings_service.get_snapshot(self.user_id)
        cycle = settings.get("cycle")
        
        if not cycle or not cycle.get("anchor"):
            return  # No cycle defined yet
        
        engine = create_calendar_engine(self.user_id)
        
        # Get anchor date - this is when the user started, don't fill before this
        anchor_date_str = cycle["anchor"].get("date")
        if not anchor_date_str:
            return
        
        anchor_date = date.fromisoformat(anchor_date_str)
        
        # Generate from anchor date to end of that year + next year
        # Don't fill days BEFORE the anchor - user didn't work then
        start_date = anchor_date
        end_date = date(anchor_date.year + 1, 12, 31)  # Through next year
        
        # Convert settings cycle to engine format
        cycle_for_engine = {
            "id": cycle.get("id"),
            "anchor_date": anchor_date_str,
            "anchor_cycle_day": cycle["anchor"].get("cycle_day", 1),
            "cycle_length": cycle.get("total_days", 15),
            "pattern": [
                {"label": block["type"], "duration": block["days"]}
                for block in cycle.get("pattern", [])
            ]
        }
        
        # Get leave blocks
        leave_blocks = [
            {"start_date": lb["start_date"], "end_date": lb["end_date"]}
            for lb in settings.get("leave_blocks", [])
        ]
        
        # Generate calendar from anchor date onward
        try:
            days = engine.generate_range(start_date, end_date, cycle_for_engine, leave_blocks)
            
            # Convert to dict for upsert
            days_data = [
                {
                    "user_id": self.user_id,
                    "date": d.date.isoformat(),
                    "cycle_id": d.cycle_id,
                    "cycle_day": d.cycle_day,
                    "work_type": d.work_type.value,
                    "state_json": d.state_json
                }
                for d in days
            ]
            
            # Delete ALL existing days for this user first
            self.db.client.table("calendar_days").delete().eq(
                "user_id", self.user_id
            ).execute()
            
            # Insert new days (only from anchor onward)
            if days_data:
                self.db.client.table("calendar_days").upsert(days_data).execute()
            
            logger.info(f"Regenerated {len(days_data)} calendar days for user {self.user_id} from {start_date}")
        except Exception as e:
            logger.error(f"Failed to regenerate calendar: {e}")


def create_command_executor(db: Database, user_id: str) -> CommandExecutor:
    """Factory function"""
    return CommandExecutor(db, user_id)

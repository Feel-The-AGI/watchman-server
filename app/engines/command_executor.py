"""
Command Executor Service
Validates commands, routes through Constraint Engine, creates proposals, executes approved commands
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, date, timedelta
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
    "override_days",  # Bulk update past/future calendar days to a specific work type
    "create_daily_log",  # Create daily notes and log hours worked
    "create_incident",  # Log workplace incidents
    "copy_incident",  # Copy an incident from one date to another
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

        # Regenerate calendar (but NOT for override_days - those are manual overrides that should persist)
        if action != "override_days":
            await self._regenerate_calendar()
        else:
            logger.info(f"Skipping calendar regeneration for override_days (manual override should persist)")

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
        elif action == "override_days":
            return await self._action_override_days(payload)
        elif action == "create_daily_log":
            return await self._action_create_daily_log(payload)
        elif action == "create_incident":
            return await self._action_create_incident(payload)
        elif action == "copy_incident":
            return await self._action_copy_incident(payload)
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
        
        # Normalize incoming anchor format - support both nested and flat
        anchor_date = None
        anchor_cycle_day = None
        
        if "anchor" in payload and isinstance(payload["anchor"], dict):
            # New nested format from chat: {anchor: {date: "...", cycle_day: N}}
            anchor_date = payload["anchor"].get("date")
            anchor_cycle_day = payload["anchor"].get("cycle_day")
        if "anchor_date" in payload:
            # Flat format: {anchor_date: "...", anchor_cycle_day: N}
            anchor_date = payload.get("anchor_date") or anchor_date
            anchor_cycle_day = payload.get("anchor_cycle_day") or anchor_cycle_day
        
        # Normalize existing anchor format
        existing_anchor_date = None
        existing_anchor_cycle_day = 1
        if isinstance(existing_cycle.get("anchor"), dict):
            existing_anchor_date = existing_cycle["anchor"].get("date")
            existing_anchor_cycle_day = existing_cycle["anchor"].get("cycle_day", 1)
        else:
            existing_anchor_date = existing_cycle.get("anchor_date")
            existing_anchor_cycle_day = existing_cycle.get("anchor_cycle_day", 1)
        
        # Normalize pattern format - ensure {label, duration}
        raw_pattern = payload.get("pattern", existing_cycle.get("pattern", []))
        normalized_pattern = []
        for block in raw_pattern:
            if "label" in block:
                normalized_pattern.append({"label": block["label"], "duration": block["duration"]})
            elif "type" in block:
                # Convert {type, days} to {label, duration}
                label = block["type"]
                if label == "day_shift":
                    label = "work_day"
                elif label == "night_shift":
                    label = "work_night"
                normalized_pattern.append({"label": label, "duration": block.get("days", block.get("duration", 5))})
            else:
                normalized_pattern.append(block)
        
        # Build normalized cycle data with flat anchor format
        cycle_data = {
            "id": payload.get("id", existing_cycle.get("id", str(uuid4()))),
            "name": payload.get("name", existing_cycle.get("name", "My Rotation")),
            "pattern": normalized_pattern,
            "anchor_date": anchor_date or existing_anchor_date,
            "anchor_cycle_day": anchor_cycle_day or existing_anchor_cycle_day,
            "total_days": sum(block.get("duration", 0) for block in normalized_pattern) or existing_cycle.get("total_days", 15)
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

    async def _action_override_days(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Bulk override calendar days to a specific work type.
        Useful for correcting past entries or setting manual overrides.

        Payload:
            start_date: str - Start of date range (YYYY-MM-DD)
            end_date: str - End of date range (YYYY-MM-DD)
            work_type: str - "work_day", "work_night", or "off"
            preserve_off_days: bool - If true, skip days that are currently "off" (default: True)
        """
        from app.engines.calendar_engine import CALENDAR_ENGINE_VERSION
        from app.models import WorkType

        logger.info(f"=== OVERRIDE_DAYS EXECUTING for user {self.user_id} ===")
        logger.info(f"Payload: {payload}")

        start_date_str = payload.get("start_date")
        end_date_str = payload.get("end_date")
        work_type_str = payload.get("work_type")
        preserve_off_days = payload.get("preserve_off_days", True)  # Default to preserving off days

        if not start_date_str or not end_date_str:
            raise ValueError("start_date and end_date are required")
        if not work_type_str:
            raise ValueError("work_type is required (work_day, work_night, or off)")

        # Normalize work_type aliases
        work_type_map = {
            "day_shift": "work_day",
            "day": "work_day",
            "work_day": "work_day",
            "night_shift": "work_night",
            "night": "work_night",
            "work_night": "work_night",
            "off": "off",
            "rest": "off",
            "blank": "blank",
            "undefined": "blank",
            "untracked": "blank",
            "unknown": "blank",
        }
        work_type = work_type_map.get(work_type_str.lower(), work_type_str)

        if work_type not in ["work_day", "work_night", "off", "blank"]:
            raise ValueError(f"Invalid work_type: {work_type_str}. Must be work_day, work_night, off, or blank")

        # Calculate available hours based on work type
        available_hours_map = {
            "work_day": 4.0,
            "work_night": 2.0,
            "off": 12.0,
            "blank": 0.0  # Blank/untracked days have no scheduled hours
        }
        available_hours = available_hours_map[work_type]

        # Fetch existing calendar days in range
        result = self.db.client.table("calendar_days").select("*").eq(
            "user_id", self.user_id
        ).gte("date", start_date_str).lte("date", end_date_str).execute()

        existing_days = {d["date"]: d for d in (result.data or [])}

        # Generate all dates in range
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)

        updated_days = []
        skipped_off_days = 0
        current = start_date
        while current <= end_date:
            date_str = current.isoformat()
            existing = existing_days.get(date_str)

            # If preserve_off_days is True and this day is currently "off", skip it
            if preserve_off_days and existing and existing.get("work_type") == "off":
                logger.debug(f"Preserving off day: {date_str}")
                skipped_off_days += 1
                current += timedelta(days=1)
                continue

            # Build updated day data
            state_json = existing.get("state_json", {}) if existing else {}
            state_json["available_hours"] = available_hours
            state_json["engine_version"] = CALENDAR_ENGINE_VERSION
            state_json["manual_override"] = True  # Flag that this was manually set

            day_data = {
                "user_id": self.user_id,
                "date": date_str,
                "cycle_id": existing.get("cycle_id") if existing else None,
                "cycle_day": existing.get("cycle_day", 1) if existing else 1,
                "work_type": work_type,
                "state_json": state_json
            }
            updated_days.append(day_data)
            current += timedelta(days=1)

        # Upsert all updated days (specify conflict columns for unique constraint)
        if updated_days:
            result = self.db.client.table("calendar_days").upsert(
                updated_days,
                on_conflict="user_id,date"
            ).execute()
            logger.info(f"Upsert result: {len(result.data) if result.data else 0} rows affected")

        logger.info(f"=== OVERRIDE_DAYS COMPLETE: {len(updated_days)} days updated, {skipped_off_days} off days preserved, from {start_date_str} to {end_date_str} set to {work_type} for user {self.user_id} ===")

        return {
            "updated_count": len(updated_days),
            "skipped_off_days": skipped_off_days,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "work_type": work_type,
            "preserve_off_days": preserve_off_days
        }

    async def _action_create_daily_log(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a daily log/note entry.

        Payload:
            date: str - Date in YYYY-MM-DD format
            note: str - The note content
            actual_hours: float (optional) - Actual hours worked
            overtime_hours: float (optional) - Overtime hours
        """
        logger.info(f"=== CREATE_DAILY_LOG for user {self.user_id} ===")
        logger.info(f"Payload: {payload}")

        date_str = payload.get("date")
        note = payload.get("note", "")
        actual_hours = payload.get("actual_hours")
        overtime_hours = payload.get("overtime_hours", 0)

        if not date_str:
            date_str = date.today().isoformat()

        if not note:
            raise ValueError("Note content is required")

        # Create the daily log
        log_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "date": date_str,
            "note": note,
            "actual_hours": actual_hours,
            "overtime_hours": overtime_hours
        }

        result = self.db.client.table("daily_logs").insert(log_data).execute()

        if result.data and len(result.data) > 0:
            log = result.data[0]
            logger.info(f"=== DAILY_LOG CREATED: id={log['id']}, date={date_str} ===")
            return {"daily_log": log}

        raise Exception("Failed to create daily log")

    async def _action_create_incident(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create an incident report.

        Payload:
            date: str - Date in YYYY-MM-DD format
            type: str - Incident type (overtime, safety, equipment, harassment, injury, policy_violation, health, etc.)
            severity: str - low, medium, high, critical
            title: str - Brief title
            description: str - Detailed description
            reported_to: str (optional) - Who was this reported to
            witnesses: str (optional) - Names of witnesses
        """
        logger.info(f"=== CREATE_INCIDENT for user {self.user_id} ===")
        logger.info(f"Payload: {payload}")

        date_str = payload.get("date")
        incident_type = payload.get("type", "other")
        severity = payload.get("severity", "medium")
        title = payload.get("title", "")
        description = payload.get("description", "")
        reported_to = payload.get("reported_to")
        witnesses = payload.get("witnesses")

        if not date_str:
            date_str = date.today().isoformat()

        if not title:
            raise ValueError("Incident title is required")
        if not description:
            raise ValueError("Incident description is required")

        # Validate incident type
        valid_types = [
            "overtime", "safety", "equipment", "harassment", "injury", "policy_violation",
            "health", "discrimination", "workload", "compensation", "scheduling",
            "communication", "retaliation", "environment", "other"
        ]
        if incident_type not in valid_types:
            logger.warning(f"Invalid incident type '{incident_type}', defaulting to 'other'")
            incident_type = "other"

        # Validate severity
        valid_severities = ["low", "medium", "high", "critical"]
        if severity not in valid_severities:
            logger.warning(f"Invalid severity '{severity}', defaulting to 'medium'")
            severity = "medium"

        # Create the incident
        incident_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "date": date_str,
            "type": incident_type,
            "severity": severity,
            "title": title,
            "description": description,
            "reported_to": reported_to,
            "witnesses": witnesses
        }

        result = self.db.client.table("incidents").insert(incident_data).execute()

        if result.data and len(result.data) > 0:
            incident = result.data[0]
            logger.info(f"=== INCIDENT CREATED: id={incident['id']}, type={incident_type}, severity={severity} ===")
            return {"incident": incident}

        raise Exception("Failed to create incident")

    async def _action_copy_incident(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Copy an incident from one date to another.

        Payload:
            source_date: str - Date to copy FROM in YYYY-MM-DD format
            target_date: str - Date to copy TO in YYYY-MM-DD format
        """
        logger.info(f"=== COPY_INCIDENT for user {self.user_id} ===")
        logger.info(f"Payload: {payload}")

        source_date = payload.get("source_date")
        target_date = payload.get("target_date")

        if not source_date:
            raise ValueError("source_date is required")
        if not target_date:
            raise ValueError("target_date is required")

        # Find incidents on the source date
        result = self.db.client.table("incidents").select("*").eq(
            "user_id", self.user_id
        ).eq("date", source_date).execute()

        if not result.data or len(result.data) == 0:
            raise ValueError(f"No incidents found on {source_date}")

        copied_incidents = []
        for incident in result.data:
            # Create a copy with new date and new ID
            new_incident = {
                "id": str(uuid4()),
                "user_id": self.user_id,
                "date": target_date,
                "type": incident["type"],
                "severity": incident["severity"],
                "title": incident["title"],
                "description": incident["description"],
                "reported_to": incident.get("reported_to"),
                "witnesses": incident.get("witnesses")
            }

            copy_result = self.db.client.table("incidents").insert(new_incident).execute()
            if copy_result.data and len(copy_result.data) > 0:
                copied_incidents.append(copy_result.data[0])

        logger.info(f"=== COPIED {len(copied_incidents)} incidents from {source_date} to {target_date} ===")
        return {
            "copied_count": len(copied_incidents),
            "source_date": source_date,
            "target_date": target_date,
            "incidents": copied_incidents
        }

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
        """Regenerate calendar days from current settings, preserving manual overrides"""
        settings = await self.settings_service.get_snapshot(self.user_id)
        cycle = settings.get("cycle")

        if not cycle:
            return  # No cycle defined yet

        # Handle both formats: {anchor: {date, cycle_day}} or {anchor_date, anchor_cycle_day}
        anchor_date_str = None
        anchor_cycle_day = 1

        if isinstance(cycle.get("anchor"), dict):
            # New format: {anchor: {date: "...", cycle_day: 1}}
            anchor_date_str = cycle["anchor"].get("date")
            anchor_cycle_day = cycle["anchor"].get("cycle_day", 1)
        else:
            # Old format: {anchor_date: "...", anchor_cycle_day: 1}
            anchor_date_str = cycle.get("anchor_date")
            anchor_cycle_day = cycle.get("anchor_cycle_day", 1)

        if not anchor_date_str:
            logger.warning(f"No anchor date for user {self.user_id}, skipping calendar regeneration")
            return

        anchor_date = date.fromisoformat(anchor_date_str)

        engine = create_calendar_engine(self.user_id)

        # Generate from anchor date to end of that year + next year
        start_date = anchor_date
        end_date = date(anchor_date.year + 1, 12, 31)

        # Convert pattern to engine format - handle both {label, duration} and {type, days}
        raw_pattern = cycle.get("pattern", [])
        engine_pattern = []
        for block in raw_pattern:
            if "label" in block:
                engine_pattern.append({"label": block["label"], "duration": block["duration"]})
            elif "type" in block:
                engine_pattern.append({"label": block["type"], "duration": block.get("days", block.get("duration", 5))})
            else:
                engine_pattern.append(block)

        cycle_for_engine = {
            "id": cycle.get("id"),
            "anchor_date": anchor_date_str,
            "anchor_cycle_day": anchor_cycle_day,
            "cycle_length": cycle.get("total_days") or sum(b.get("duration", b.get("days", 0)) for b in raw_pattern),
            "pattern": engine_pattern
        }

        # Get leave blocks
        leave_blocks = [
            {"start_date": lb["start_date"], "end_date": lb["end_date"]}
            for lb in settings.get("leave_blocks", [])
        ]

        # Generate calendar from anchor date onward
        try:
            days = engine.generate_range(start_date, end_date, cycle_for_engine, leave_blocks)

            # IMPORTANT: Fetch existing days that have manual_override flag to preserve them
            existing_result = self.db.client.table("calendar_days").select("date, state_json, work_type").eq(
                "user_id", self.user_id
            ).gte("date", start_date.isoformat()).execute()

            # Build map of manually overridden days to preserve
            manual_override_days = {}
            for existing_day in (existing_result.data or []):
                state = existing_day.get("state_json", {})
                if state.get("manual_override"):
                    manual_override_days[existing_day["date"]] = existing_day
                    logger.debug(f"Preserving manual override for date {existing_day['date']}")

            if manual_override_days:
                logger.info(f"Preserving {len(manual_override_days)} manually overridden days during regeneration")

            # Convert to dict for upsert, but preserve manual overrides
            days_data = []
            for d in days:
                date_str = d.date.isoformat()

                # Check if this day has a manual override
                if date_str in manual_override_days:
                    # Keep the manually overridden version
                    override = manual_override_days[date_str]
                    days_data.append({
                        "user_id": self.user_id,
                        "date": date_str,
                        "cycle_id": d.cycle_id,
                        "cycle_day": d.cycle_day,  # Update cycle_day for reference
                        "work_type": override["work_type"],  # Keep overridden work_type
                        "state_json": override["state_json"]  # Keep overridden state
                    })
                else:
                    # Use freshly generated day
                    days_data.append({
                        "user_id": self.user_id,
                        "date": date_str,
                        "cycle_id": d.cycle_id,
                        "cycle_day": d.cycle_day,
                        "work_type": d.work_type.value,
                        "state_json": d.state_json
                    })

            # Delete from anchor forward only
            self.db.client.table("calendar_days").delete().eq(
                "user_id", self.user_id
            ).gte("date", start_date.isoformat()).execute()

            # Insert new days (including preserved manual overrides)
            if days_data:
                self.db.client.table("calendar_days").upsert(days_data).execute()

            logger.info(f"Regenerated {len(days_data)} calendar days for user {self.user_id} from {start_date} (preserved {len(manual_override_days)} manual overrides)")
        except Exception as e:
            logger.error(f"Failed to regenerate calendar: {e}")


def create_command_executor(db: Database, user_id: str) -> CommandExecutor:
    """Factory function"""
    return CommandExecutor(db, user_id)

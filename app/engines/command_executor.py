"""
Command Executor Service
Validates commands, routes through Constraint Engine, creates proposals, executes approved commands
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, date, timedelta, timezone
from uuid import uuid4
from loguru import logger

from app.commitment_limit import (
    COMMITMENT_LIMIT_REPLY,
    free_commitment_limit_hit,
    reused_commitment,
)
from app.database import Database
from app.engines.master_settings_service import MasterSettingsService
from app.engines.calendar_engine import (
    CALENDAR_ENGINE_VERSION,
    block_day_for,
    create_calendar_engine,
    generation_bounds,
    shift_badge,
)
from app.engines.calendar_authority import (
    commitment_is_study,
    effective_constraints,
    is_night_study_rule,
    night_shift_dates,
    sync_cycle_row,
)
from app.leave_access import LEAVE_PRO_REPLY, leave_blocked
from app.onboarding import finish_onboarding_for_saved_cycle
from app.prompts.registry import insert_row
from app.engines.proposal_effects import (
    card_command,
    commitment_record,
    event_dates,
    leave_record,
    stamp_day_state,
)


# Command action types
# A failed commitment delete records ids here until a calendar read finishes the rebuild.
PENDING_COMMITMENT_REMOVAL = "commitments_pending_removal"
PENDING_COMMITMENT_ADD = "commitments_pending_add"


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


def cycle_anchor(payload: dict) -> Tuple[Optional[str], Any]:
    """Incoming anchor date and cycle day. A flat anchor_date wins over nested."""
    anchor_date = None
    anchor_cycle_day = None
    anchor = payload.get("anchor") if isinstance(payload, dict) else None
    if isinstance(anchor, dict):
        anchor_date = anchor.get("date")
        anchor_cycle_day = anchor.get("cycle_day")
    if isinstance(payload, dict) and "anchor_date" in payload:
        anchor_date = payload.get("anchor_date") or anchor_date
        anchor_cycle_day = payload.get("anchor_cycle_day") or anchor_cycle_day
    return anchor_date, anchor_cycle_day


def cycle_shift_days(payload: dict) -> Optional[int]:
    """Days to add to the anchor. None leaves the anchor date alone.

    An explicit anchor date, flat or nested, wins over a shift. A bool, a
    fractional number, a non-numeric string, zero, or a shift past 366 days
    is ignored.
    """
    shift = payload.get("shift_by_days") if isinstance(payload, dict) else None
    explicit, _cycle_day = cycle_anchor(payload or {})
    if explicit not in (None, ""):
        if shift not in (None, ""):
            logger.warning(
                f"update_cycle kept anchor_date={explicit!r} "
                f"and ignored shift_by_days={shift!r}"
            )
        return None
    return _parsed_shift_days(shift)


def _parsed_shift_days(shift) -> Optional[int]:
    if shift in (None, ""):
        return None
    # bool is a subclass of int. True would otherwise become 1.
    if isinstance(shift, bool):
        logger.warning(f"update_cycle ignored shift_by_days={shift!r}")
        return None
    count = None
    if isinstance(shift, int):
        count = shift
    elif isinstance(shift, float):
        if not shift.is_integer():
            logger.warning(f"update_cycle ignored shift_by_days={shift!r}")
            return None
        count = int(shift)
    elif isinstance(shift, str):
        try:
            count = int(shift.strip())
        except ValueError:
            logger.warning(f"update_cycle ignored shift_by_days={shift!r}")
            return None
    else:
        logger.warning(f"update_cycle ignored shift_by_days={shift!r}")
        return None
    if count == 0:
        return None
    if abs(count) > 366:
        logger.warning(
            f"update_cycle ignored shift_by_days={count} because it is past 366 days"
        )
        return None
    return count


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
        message_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a proposal for user approval.
        
        Args:
            command: The command to propose
            message_id: Optional chat message ID that triggered this
            
        Returns:
            The created proposal
        """
        # The stored card text is the payload summary, not the model's sentence.
        command = card_command(command)
        action = command.get("action")
        payload = command.get("payload") or {}
        if leave_blocked(getattr(self, "effective_tier", None) or "", action, payload):
            raise ValueError(LEAVE_PRO_REPLY)
        if action == "add_commitment":
            limit_reply = await self.commitment_limit_message(payload)
            if limit_reply:
                raise ValueError(limit_reply)
        # Validate first
        validation = await self.validate_command(command)
        if prompt_version:
            validation = {**validation, "prompt_version": prompt_version}
            logger.info(
                f"Proposal for user {self.user_id} uses prompt_version={prompt_version}"
            )
        
        # Create proposal record. Expired proposals cannot be approved.
        proposal_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "command": command,
            "validation": validation,
            "status": "pending",
            "message_id": message_id,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        }
        if prompt_version:
            proposal_data["prompt_version"] = prompt_version
        
        result = insert_row(self.db.client, "proposals", proposal_data)
        
        if result.data and len(result.data) > 0:
            proposal = result.data[0]
            if prompt_version and not proposal.get("prompt_version"):
                proposal = {**proposal, "prompt_version": prompt_version}
            logger.info(f"Created proposal {proposal['id']} for user {self.user_id}")
            return proposal
        
        raise Exception("Failed to create proposal")
    
    async def execute(
        self, 
        command: Dict[str, Any],
        source: str = "chat",
        message_id: Optional[str] = None,
        skip_validation: bool = False,
        proposal_id: Optional[str] = None,
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
        self._proposal_id = proposal_id
        
        if action not in VALID_ACTIONS:
            raise ValueError(f"Unknown action: {action}")
        if leave_blocked(getattr(self, "effective_tier", None) or "", action, payload):
            return {"success": False, "error": LEAVE_PRO_REPLY, "code": "leave_pro"}
        if action == "add_commitment":
            limit_reply = await self.commitment_limit_message(payload)
            if limit_reply:
                return {"success": False, "error": limit_reply, "code": "commitment_limit"}

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
        
        # Project onto calendar_days before recording success. A failed write
        # must not look applied: the review route turns that into an error and
        # puts the proposal back to pending. Drop a row inserted in this call
        # so a failed calendar write does not leave an orphan.
        try:
            if action != "override_days":
                wrote = await self._regenerate_calendar()
                if not wrote:
                    await self._project_saved_events()
            else:
                logger.info("Skipping calendar regeneration for override_days (manual override should persist)")
        except Exception as e:
            logger.error(f"Calendar projection failed: {e}")
            await self._drop_inserted_row(result)
            return {
                "success": False,
                "error": str(e),
            }

        # Get after state
        after_state = await self.settings_service.get_snapshot(self.user_id)
        
        # Log the command
        try:
            command_log = await self._log_command(
                action=action,
                payload=payload,
                before_state=before_state,
                after_state=after_state,
                source=source,
                message_id=message_id,
                explanation=explanation,
            )
        except Exception as e:
            logger.error(f"Command log failed after the change was saved: {e}")
            return {
                "success": False,
                "error": str(e),
            }

        return {
            "success": True,
            "command_id": command_log["id"],
            "result": result
        }

    async def _drop_inserted_row(self, result: Any) -> None:
        """Remove the commitment or leave inserted by a command whose calendar write failed."""
        if not isinstance(result, dict):
            return
        commitment_id = result.get("inserted_commitment_id")
        leave_id = result.get("inserted_leave_id")
        try:
            if commitment_id:
                await self.db.delete_commitment(commitment_id, self.user_id)
                await self.settings_service.remove_from_list(
                    self.user_id, "commitments", commitment_id
                )
            if leave_id:
                await self.db.delete_leave_block(leave_id, self.user_id)
                await self.settings_service.remove_from_list(
                    self.user_id, "leave_blocks", leave_id
                )
        except Exception as exc:
            logger.error(f"Could not remove orphan row after a failed calendar write: {exc}")
    
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
        anchor_date, anchor_cycle_day = cycle_anchor(payload)
        
        # Normalize existing anchor format
        existing_anchor_date = None
        existing_anchor_cycle_day = 1
        if isinstance(existing_cycle.get("anchor"), dict):
            existing_anchor_date = existing_cycle["anchor"].get("date")
            existing_anchor_cycle_day = existing_cycle["anchor"].get("cycle_day", 1)
        else:
            existing_anchor_date = existing_cycle.get("anchor_date")
            existing_anchor_cycle_day = existing_cycle.get("anchor_cycle_day", 1)

        anchor_date = anchor_date or existing_anchor_date
        anchor_cycle_day = anchor_cycle_day or existing_anchor_cycle_day
        # shift_by_days moves the anchor date. Positive is later on the calendar.
        # It applies only when the payload did not send its own anchor_date.
        count = cycle_shift_days(payload)
        if count and anchor_date:
            base = date.fromisoformat(str(anchor_date)[:10])
            anchor_date = (base + timedelta(days=count)).isoformat()
        
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
        
        # Build normalized cycle data with flat anchor format.
        # A missing id stays missing. Inventing one would match no cycles row,
        # so the single active row could not be synced.
        cycle_id = payload.get("id") or existing_cycle.get("id")
        cycle_data = {
            "name": payload.get("name", existing_cycle.get("name", "My Rotation")),
            "pattern": normalized_pattern,
            "anchor_date": anchor_date,
            "anchor_cycle_day": anchor_cycle_day,
            "total_days": sum(block.get("duration", 0) for block in normalized_pattern) or existing_cycle.get("total_days", 15)
        }
        if cycle_id not in (None, ""):
            cycle_data["id"] = cycle_id
        
        await self.settings_service.update_section(self.user_id, "cycle", cycle_data)
        await sync_cycle_row(self.db, self.user_id, cycle_data)
        await finish_onboarding_for_saved_cycle(self.db, self.user_id)
        
        # Also update work settings if provided
        if "shift_hours" in payload:
            work = current.get("settings", {}).get("work", {})
            work["shift_hours"] = payload.get("shift_hours", work.get("shift_hours", 12))
            work["shift_start"] = payload.get("shift_start", work.get("shift_start", "06:00"))
            await self.settings_service.update_section(self.user_id, "work", work)
        
        return {"cycle": cycle_data}
    
    async def _action_add_commitment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a commitment row and keep the settings document in step."""
        record = commitment_record(
            self.user_id,
            payload,
            payload.get("id") or str(uuid4()),
            proposal_id=getattr(self, "_proposal_id", None),
        )
        if await self.commitment_limit_message(payload):
            raise ValueError(COMMITMENT_LIMIT_REPLY)
        saved = await self._matching_commitment(record)
        inserted_id = None
        if saved is None:
            saved = await self.db.create_commitment(record)
            if saved:
                inserted_id = saved.get("id")
        if not saved:
            raise RuntimeError(f"Commitment {record['name']!r} was not saved")
        await self.settings_service.add_to_list(self.user_id, "commitments", saved)
        return {"commitment": saved, "inserted_commitment_id": inserted_id}

    async def _account_tier(self) -> Optional[str]:
        """Stored tier, matching the commitments route. Unknown stays unlimited."""
        getter = getattr(self.db, "get_user_by_id", None)
        if getter is not None:
            try:
                user = await getter(self.user_id)
            except Exception as exc:
                logger.warning(f"Could not read tier for commitment limit {self.user_id}: {exc}")
                user = None
            if isinstance(user, dict):
                return user.get("tier") or "free"
        explicit = getattr(self, "effective_tier", None)
        if isinstance(explicit, str) and explicit:
            return explicit
        return None

    async def commitment_limit_message(self, payload: dict) -> Optional[str]:
        """Plain reply when a free account would store a third commitment."""
        tier = await self._account_tier()
        if tier != "free":
            return None
        reader = getattr(self.db, "read_commitments", None) or getattr(self.db, "get_commitments", None)
        if reader is None:
            return None
        rows = await reader(self.user_id) or []
        if not isinstance(rows, list):
            return None
        reused = reused_commitment(rows, self.user_id, payload or {})
        if free_commitment_limit_hit(tier, rows, reused):
            return COMMITMENT_LIMIT_REPLY
        return None

    async def _matching_commitment(self, record: Dict[str, Any]) -> Optional[dict]:
        rows = await self.db.get_commitments(self.user_id) or []
        source = record.get("source_text")
        start = str(record.get("start_date") or "")[:10]
        for row in rows:
            if source and str(row.get("source_text") or "") == str(source):
                return row
            if (
                row.get("name") == record.get("name")
                and str(row.get("start_date") or "")[:10] == start
                and row.get("type") == record.get("type")
            ):
                return row
        return None
    
    async def _action_remove_commitment(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a commitment"""
        commitment_id = payload.get("id")
        if not commitment_id:
            raise ValueError("Commitment ID required")

        deleted = await self.db.delete_commitment(commitment_id, self.user_id)
        if deleted is False:
            raise RuntimeError("Commitment could not be removed")
        await self.settings_service.remove_from_list(self.user_id, "commitments", commitment_id)
        return {"removed_id": commitment_id}
    
    async def _action_add_leave(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Insert a leave_blocks row. Payload type stays in effects, not commitments.type."""
        record = leave_record(
            self.user_id,
            payload,
            payload.get("id") or str(uuid4()),
            proposal_id=getattr(self, "_proposal_id", None),
        )
        saved = await self._matching_leave(record)
        inserted_id = None
        if saved is None:
            saved = await self.db.create_leave_block(record)
            if saved:
                inserted_id = saved.get("id")
        if not saved:
            raise RuntimeError("Leave was not saved")
        await self.settings_service.add_to_list(self.user_id, "leave_blocks", saved)
        return {"leave": saved, "inserted_leave_id": inserted_id}

    async def _matching_leave(self, record: Dict[str, Any]) -> Optional[dict]:
        rows = await self.db.get_leave_blocks(self.user_id) or []
        proposal_id = (record.get("effects") or {}).get("proposal_id")
        for row in rows:
            effects = row.get("effects") or {}
            if proposal_id and effects.get("proposal_id") == proposal_id:
                return row
            if (
                row.get("name") == record.get("name")
                and str(row.get("start_date") or "")[:10] == record.get("start_date")
                and str(row.get("end_date") or "")[:10] == record.get("end_date")
            ):
                return row
        return None
    
    async def _action_remove_leave(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Remove a leave block"""
        leave_id = payload.get("id")
        if not leave_id:
            raise ValueError("Leave ID required")

        deleted = await self.db.delete_leave_block(leave_id, self.user_id)
        if deleted is False:
            raise RuntimeError("Leave could not be removed")
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
            cycle = before_state.get("cycle") if isinstance(before_state, dict) else None
            await sync_cycle_row(self.db, self.user_id, cycle)
        
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
            cycle = after_state.get("cycle") if isinstance(after_state, dict) else None
            await sync_cycle_row(self.db, self.user_id, cycle)
        
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
        
        constraints = await effective_constraints(
            self.db, self.user_id, current_settings.get("constraints", [])
        )
        
        # For add_commitment, check against constraints
        if action == "add_commitment" and commitment_is_study(payload):
            night_rule = next((item for item in constraints if is_night_study_rule(item)), None)
            if night_rule:
                nights = night_shift_dates(current_settings.get("cycle"), payload)
                if nights:
                    shown = ", ".join(nights[:5])
                    if len(nights) > 5:
                        shown = f"{shown} and {len(nights) - 5} more"
                    violations.append({
                        "constraint_id": night_rule.get("id"),
                        "rule": "no_activity_on",
                        "message": f"Study is not allowed on night shifts ({shown})",
                        "type": "hard",
                        "dates": nights,
                    })
                else:
                    warnings.append({
                        "constraint_id": night_rule.get("id"),
                        "rule": "no_activity_on",
                        "message": "Study stays off night shifts. This commitment is not placed on those days.",
                        "type": "soft",
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
    
    async def finish_pending_removals(self) -> bool:
        """Rebuild for commitment ids a failed delete or create left behind.

        Opening the year finishes that calendar write, then clears the ids.
        A failed rebuild leaves them in place.
        """
        current = await self.settings_service.get(self.user_id)
        settings = current.get("settings") if isinstance(current.get("settings"), dict) else {}
        pending_removal = _pending_ids(settings.get(PENDING_COMMITMENT_REMOVAL))
        pending_add = _pending_ids(settings.get(PENDING_COMMITMENT_ADD))
        if not pending_removal and not pending_add:
            return False
        wrote = await self._regenerate_calendar()
        if not wrote:
            await self._project_saved_events()
        fresh = await self.settings_service.get(self.user_id)
        updated = dict(fresh.get("settings") or {})
        updated[PENDING_COMMITMENT_REMOVAL] = _without_ids(
            updated.get(PENDING_COMMITMENT_REMOVAL), pending_removal
        )
        updated[PENDING_COMMITMENT_ADD] = _without_ids(
            updated.get(PENDING_COMMITMENT_ADD), pending_add
        )
        await self.settings_service.update(self.user_id, updated, fresh["version"])
        return True

    async def _project_saved_events(self) -> None:
        """Paint saved rows onto existing days when no cycle can rebuild them.

        Commitment and leave reads raise on failure, so a missed read does not
        look like an empty list and wipe the calendar. A chip whose commitment
        is gone is dropped.
        """
        table_rows = await self.db.read_commitments(self.user_id)
        leaves = await self.db.read_leave_blocks(self.user_id)
        snapshot = await self.settings_service.get_snapshot(self.user_id)
        commitments = _commitments_to_stamp(snapshot.get("commitments") or [], table_rows)
        dates = set(event_dates(commitments, leaves))
        by_date, chip_days = await self._days_with_commitment_chips(dates)
        dates.update(chip_days)
        if not dates:
            return
        updates = []
        for day in sorted(dates):
            key = day.isoformat()
            current = by_date.get(key) or {}
            base = _without_removed_commitments(current.get("state_json"), commitments)
            if not current.get("state_json"):
                base.update({
                    "available_hours": 12.0,
                    "used_hours": 0,
                    "is_overloaded": False,
                    "is_leave": False,
                    "tags": [],
                    "engine_version": CALENDAR_ENGINE_VERSION,
                })
            updates.append({
                "user_id": self.user_id,
                "date": key,
                "cycle_id": current.get("cycle_id"),
                "cycle_day": current.get("cycle_day"),
                "work_type": current.get("work_type") or "off",
                "state_json": stamp_day_state(base, day, commitments, leaves),
            })
        saved = await self.db.upsert_calendar_days(updates)
        if not saved:
            raise RuntimeError("Calendar was not updated")

    async def _days_with_commitment_chips(self, event_days: set):
        """Stored days that already show a commitment, so a deleted one can be cleared."""
        today = date.today()
        years = set(range((today - timedelta(days=400)).year, (today + timedelta(days=800)).year + 1))
        for day in event_days:
            years.add(day.year)
        by_date = {}
        found = set()
        for year in sorted(years):
            rows = await self.db.read_calendar_days(
                self.user_id, date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()
            )
            for row in rows or []:
                key = _day_key(row.get("date"))
                by_date[key] = row
                state = row.get("state_json") or {}
                if not state.get("commitments"):
                    continue
                parsed = _parse_day(key)
                if parsed is not None:
                    found.add(parsed)
        return by_date, found

    async def _regenerate_calendar(self) -> bool:
        """Rebuild from 1 Jan of the anchor year through the next year."""
        settings = await self.settings_service.get_snapshot(self.user_id)
        cycle = settings.get("cycle") or {}
        anchor_date_str = None
        if isinstance(cycle.get("anchor"), dict):
            anchor_date_str = cycle["anchor"].get("date")
        anchor_date_str = cycle.get("anchor_date") or anchor_date_str
        if not anchor_date_str:
            return False
        anchor_date = date.fromisoformat(str(anchor_date_str)[:10])
        start_date, end_date = generation_bounds(anchor_date)
        return await self.rebuild_calendar(start_date, end_date)

    async def rebuild_calendar(self, start_date: date, end_date: date) -> bool:
        """Rewrite one inclusive range from the cycle.

        Leave in the settings document and leave_blocks are both applied.
        Commitments are stamped back onto the days. A manual override, including
        a blank mark and a day before the anchor, keeps its work type.
        """
        settings = await self.settings_service.get_snapshot(self.user_id)
        cycle = settings.get("cycle")

        if not cycle:
            return False

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
            return False

        engine = create_calendar_engine(self.user_id)

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

        # These reads raise. A swallowed empty list would look like "nothing
        # saved" and the write below would clear chips and leave.
        table_leaves = await self.db.read_leave_blocks(self.user_id)
        leave_blocks = _merge_leave_blocks(settings.get("leave_blocks") or [], table_leaves)
        table_commitments = await self.db.read_commitments(self.user_id)
        commitments = _commitments_to_stamp(settings.get("commitments") or [], table_commitments)

        constraints = await effective_constraints(
            self.db, self.user_id, settings.get("constraints", [])
        )

        # Generate calendar from anchor date onward. Failures propagate so the
        # approval is not reported as applied when the calendar write did not happen.
        days = engine.generate_range(
            start_date, end_date, cycle_for_engine, leave_blocks, constraints
        )

        existing_days = await self._read_existing_days(start_date, end_date)
        existing_by_date = {}
        manual_override_days = {}
        for existing_day in existing_days:
            key = _day_key(existing_day.get("date"))
            existing_by_date[key] = existing_day
            state = existing_day.get("state_json") or {}
            if isinstance(state, dict) and state.get("manual_override"):
                manual_override_days[key] = existing_day

        if manual_override_days:
            logger.info(f"Preserving {len(manual_override_days)} manually overridden days during regeneration")

        days_data = []
        written = set()
        for d in days:
            date_str = d.date.isoformat()
            override = manual_override_days.get(date_str)
            if override:
                base_state = dict(override.get("state_json") or {})
                work_type = override.get("work_type") or d.work_type.value
                cycle_day = override.get("cycle_day") if override.get("cycle_day") is not None else d.cycle_day
                previous_chips = base_state.get("commitments") or []
            else:
                base_state = dict(d.state_json)
                previous = (existing_by_date.get(date_str) or {}).get("state_json") or {}
                previous_chips = previous.get("commitments") or []
                work_type = d.work_type.value
                cycle_day = d.cycle_day
            # Chips with an id are re-stamped from the current commitment list.
            # A removed commitment therefore leaves the day. Chips with no id stay.
            base_state["commitments"] = [
                chip for chip in previous_chips
                if isinstance(chip, dict) and not chip.get("commitment_id")
            ]
            state = stamp_day_state(base_state, d.date, commitments, leave_blocks)
            block_day = block_day_for(d.cycle_day, engine_pattern)
            state["engine_version"] = CALENDAR_ENGINE_VERSION
            state["block_day"] = block_day
            state["shift_badge"] = shift_badge(str(work_type), block_day)
            if override:
                state["manual_override"] = True
            days_data.append({
                "user_id": self.user_id,
                "date": date_str,
                "cycle_id": (override or {}).get("cycle_id") or d.cycle_id,
                "cycle_day": cycle_day,
                "work_type": work_type,
                "state_json": state,
            })
            written.add(date_str)

        # An override the generator did not cover is still included. The write
        # is one upsert, so a failure leaves the stored rows in place.
        for key, override in manual_override_days.items():
            if key in written:
                continue
            state = dict(override.get("state_json") or {})
            state["manual_override"] = True
            state["engine_version"] = CALENDAR_ENGINE_VERSION
            days_data.append({
                "user_id": self.user_id,
                "date": key,
                "cycle_id": override.get("cycle_id"),
                "cycle_day": override.get("cycle_day"),
                "work_type": override.get("work_type"),
                "state_json": state,
            })

        if days_data:
            saved = await self.db.upsert_calendar_days(days_data)
            if not saved:
                raise RuntimeError("Calendar was not updated")

        logger.info(
            f"Regenerated {len(days_data)} calendar days for user {self.user_id} "
            f"from {start_date} (preserved {len(manual_override_days)} manual overrides)"
        )
        return True

    async def _read_existing_days(self, start_date: date, end_date: date) -> list:
        """Read the range one calendar year at a time.

        A single Supabase read stops at 1,000 rows. A rebuild from 2025 through
        the following years is longer than that, so a one-shot read would drop
        overrides past the cap. A failed read raises and nothing is written.
        """
        rows = []
        year = start_date.year
        while year <= end_date.year:
            chunk_start = max(start_date, date(year, 1, 1))
            chunk_end = min(end_date, date(year, 12, 31))
            batch = await self.db.read_calendar_days(
                self.user_id, chunk_start.isoformat(), chunk_end.isoformat()
            )
            rows.extend(batch or [])
            year += 1
        return rows


def _pending_ids(items) -> list:
    return [str(item) for item in (items or []) if str(item).strip()]


def _without_ids(items, finished: list) -> list:
    done = set(finished)
    return [item for item in (items or []) if str(item) not in done]


def _without_removed_commitments(state, commitments: list) -> dict:
    base = dict(state or {})
    current_ids = {
        item.get("id") for item in commitments or []
        if isinstance(item, dict) and item.get("id")
    }
    kept = []
    for chip in base.get("commitments") or []:
        if not isinstance(chip, dict):
            continue
        chip_id = chip.get("commitment_id")
        if chip_id and chip_id not in current_ids:
            continue
        kept.append(chip)
    base["commitments"] = kept
    return base


def _parse_day(value: str):
    if len(value or "") < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _day_key(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value or "")[:10]


def _commitments_to_stamp(document_rows: list, table_rows: list) -> list:
    """Merge both copies. A status stored on the table row wins for that id."""
    statuses = {}
    for row in table_rows or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        status = row.get("status")
        if status is not None and str(status).strip() != "":
            statuses[str(row["id"])] = status
    merged = []
    for row in _merge_records(document_rows, table_rows):
        row = dict(row)
        known = statuses.get(str(row.get("id") or ""))
        if known is not None:
            row["status"] = known
        merged.append(row)
    return merged


def _dated_record(row: dict) -> dict:
    start = _day_key(row.get("start_date") or row.get("date"))
    end = _day_key(row.get("end_date")) or start
    if start:
        return {**row, "start_date": start, "end_date": end}
    return dict(row)


def _merge_records(document_rows: list, table_rows: list) -> list:
    """One row per id. The table copy replaces the document copy.

    A date edit used to leave both spans, because the old key included the
    dates and the name. Rows with no id are still deduped by span and name.
    """
    by_id = {}
    order = []
    anonymous = []
    seen_anonymous = set()

    def take_identified(row: dict, overwrite: bool) -> bool:
        row_id = str(row.get("id") or "")
        if not row_id:
            return False
        if row_id not in by_id:
            order.append(row_id)
        if row_id not in by_id or overwrite:
            by_id[row_id] = _dated_record(row)
        return True

    def take_anonymous(row: dict) -> None:
        start = _day_key(row.get("start_date") or row.get("date"))
        end = _day_key(row.get("end_date")) or start
        key = (start, end, str(row.get("name") or ""))
        if key in seen_anonymous:
            return
        seen_anonymous.add(key)
        anonymous.append(_dated_record(row))

    for row in document_rows or []:
        if isinstance(row, dict) and not take_identified(row, overwrite=False):
            take_anonymous(row)
    for row in table_rows or []:
        if isinstance(row, dict) and not take_identified(row, overwrite=True):
            take_anonymous(row)
    return [by_id[row_id] for row_id in order] + anonymous


def _merge_leave_blocks(document_leaves: list, table_leaves: list) -> list:
    merged = []
    seen = set()
    for block in list(document_leaves or []) + list(table_leaves or []):
        if not isinstance(block, dict):
            continue
        start = _day_key(block.get("start_date") or block.get("date"))
        end = _day_key(block.get("end_date")) or start
        if not start:
            continue
        key = (start, end, str(block.get("id") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append({**block, "start_date": start, "end_date": end})
    return merged


def create_command_executor(db: Database, user_id: str) -> CommandExecutor:
    """Factory function"""
    return CommandExecutor(db, user_id)

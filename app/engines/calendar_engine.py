"""
Watchman Calendar Engine
Core engine for generating and managing calendar state
Handles cycle projection, day-by-day map generation, and anchor logic
"""

from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple
from loguru import logger
import hashlib
import json

from app.models import (
    WorkType, CalendarDayCreate
)


class CalendarEngine:
    """
    The Calendar Engine is the deterministic core of Watchman.
    It generates day-by-day maps from cycle definitions and never involves AI.
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
    
    def calculate_cycle_day(
        self,
        target_date: date,
        anchor_date: date,
        anchor_cycle_day: int,
        cycle_length: int
    ) -> int:
        """
        Calculate which day of the cycle a given date falls on.
        
        Args:
            target_date: The date to calculate for
            anchor_date: Known reference date
            anchor_cycle_day: Which cycle day the anchor date represents (1-indexed)
            cycle_length: Total length of the cycle in days
        
        Returns:
            The cycle day (1-indexed) for the target date
        """
        days_diff = (target_date - anchor_date).days
        # Adjust for 1-indexed cycle days
        cycle_day = ((anchor_cycle_day - 1 + days_diff) % cycle_length) + 1
        return cycle_day
    
    def get_work_type_for_cycle_day(
        self,
        cycle_day: int,
        pattern: List[Dict]
    ) -> WorkType:
        """
        Determine work type for a given cycle day based on pattern.
        
        Args:
            cycle_day: The day of the cycle (1-indexed)
            pattern: List of cycle blocks with label and duration
        
        Returns:
            The WorkType for that cycle day
        """
        day_counter = 0
        for block in pattern:
            day_counter += block["duration"]
            if cycle_day <= day_counter:
                return WorkType(block["label"])
        
        # Fallback (shouldn't happen if pattern is valid)
        return WorkType.OFF
    
    def generate_year(
        self,
        year: int,
        cycle: Dict,
        leave_blocks: Optional[List[Dict]] = None
    ) -> List[CalendarDayCreate]:
        """
        Generate a full year of calendar days based on cycle definition.
        
        Args:
            year: The year to generate (e.g., 2026)
            cycle: The cycle definition with pattern, anchor_date, anchor_cycle_day
            leave_blocks: Optional list of leave blocks to apply
        
        Returns:
            List of CalendarDayCreate objects for the entire year
        """
        start_date = date(year, 1, 1)
        end_date = date(year, 12, 31)
        
        return self.generate_range(start_date, end_date, cycle, leave_blocks)
    
    def generate_range(
        self,
        start_date: date,
        end_date: date,
        cycle: Dict,
        leave_blocks: Optional[List[Dict]] = None
    ) -> List[CalendarDayCreate]:
        """
        Generate calendar days for a date range.
        
        Args:
            start_date: Start of the range
            end_date: End of the range (inclusive)
            cycle: The cycle definition
            leave_blocks: Optional list of leave blocks
        
        Returns:
            List of CalendarDayCreate objects
        """
        leave_dates = self._build_leave_date_set(leave_blocks) if leave_blocks else set()
        
        anchor_date = date.fromisoformat(cycle["anchor_date"]) if isinstance(cycle["anchor_date"], str) else cycle["anchor_date"]
        anchor_cycle_day = cycle["anchor_cycle_day"]
        cycle_length = cycle["cycle_length"]
        pattern = cycle["pattern"]
        cycle_id = cycle.get("id")
        
        days = []
        current_date = start_date
        
        while current_date <= end_date:
            cycle_day = self.calculate_cycle_day(
                current_date, anchor_date, anchor_cycle_day, cycle_length
            )
            
            work_type = self.get_work_type_for_cycle_day(cycle_day, pattern)
            
            # Check if this day is a leave day
            is_leave = current_date in leave_dates
            
            # Build initial state
            state = {
                "commitments": [],
                "available_hours": self._get_available_hours(work_type, is_leave),
                "used_hours": 0,
                "is_overloaded": False,
                "is_leave": is_leave,
                "tags": []
            }
            
            if is_leave:
                state["tags"].append("leave")
            
            day = CalendarDayCreate(
                user_id=self.user_id,
                date=current_date,
                cycle_id=cycle_id,
                cycle_day=cycle_day,
                work_type=work_type,
                state_json=state
            )
            
            days.append(day)
            current_date += timedelta(days=1)
        
        logger.info(f"Generated {len(days)} calendar days from {start_date} to {end_date}")
        return days
    
    def _build_leave_date_set(self, leave_blocks: List[Dict]) -> set:
        """Build a set of dates that are leave days"""
        leave_dates = set()
        
        for block in leave_blocks:
            start = date.fromisoformat(block["start_date"]) if isinstance(block["start_date"], str) else block["start_date"]
            end = date.fromisoformat(block["end_date"]) if isinstance(block["end_date"], str) else block["end_date"]
            
            current = start
            while current <= end:
                leave_dates.add(current)
                current += timedelta(days=1)
        
        return leave_dates
    
    def _get_available_hours(self, work_type: WorkType, is_leave: bool) -> float:
        """
        Calculate available hours for non-work activities based on work type.
        
        Args:
            work_type: The type of work day
            is_leave: Whether this is a leave day
        
        Returns:
            Available hours for personal activities
        """
        if is_leave:
            return 16.0  # Full day available during leave
        
        if work_type == WorkType.OFF:
            return 12.0  # Off day - most time available
        elif work_type == WorkType.WORK_DAY:
            return 4.0  # Day shift - evening hours available
        elif work_type == WorkType.WORK_NIGHT:
            return 2.0  # Night shift - minimal time available
        
        return 0.0
    
    def apply_commitments(
        self,
        days: List[Dict],
        commitments: List[Dict],
        constraints: List[Dict]
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        Apply commitments to calendar days respecting constraints.
        
        Args:
            days: List of calendar day dictionaries
            commitments: List of commitment dictionaries
            constraints: List of constraint dictionaries
        
        Returns:
            Tuple of (updated_days, violations)
        """
        violations = []
        days_map = {d["date"]: d for d in days}
        
        for commitment in commitments:
            if commitment.get("status") != "active":
                continue
            
            commitment_type = commitment.get("type")
            constraints_json = commitment.get("constraints_json", {})
            
            # Get study days based on commitment constraints
            study_on = constraints_json.get("study_on", ["off"])
            exclude = constraints_json.get("exclude", ["work_night"])
            _frequency = constraints_json.get("frequency", "weekly")  # noqa: F841 - reserved for future use
            duration_hours = constraints_json.get("duration_hours", 2.0)
            
            # Apply commitment to appropriate days
            for date_str, day in days_map.items():
                work_type = day.get("work_type", "off")
                state = day.get("state_json", {})
                
                # Check if this day type is allowed
                should_apply = False
                if "off" in study_on and work_type == "off":
                    should_apply = True
                elif "work_day_evening" in study_on and work_type == "work_day":
                    should_apply = True
                
                # Check exclusions
                if work_type in exclude:
                    should_apply = False
                
                if should_apply:
                    # Check system constraints
                    for constraint in constraints:
                        rule = constraint.get("rule", {})
                        if rule.get("type") == "no_activity_on":
                            if work_type in rule.get("work_types", []):
                                should_apply = False
                                break
                
                if should_apply:
                    # Add commitment to day
                    day_commitment = {
                        "commitment_id": commitment["id"],
                        "name": commitment["name"],
                        "type": commitment_type,
                        "hours": duration_hours,
                        "is_preview": False
                    }
                    
                    state_commitments = state.get("commitments", [])
                    state_commitments.append(day_commitment)
                    
                    state["commitments"] = state_commitments
                    state["used_hours"] = state.get("used_hours", 0) + duration_hours
                    
                    # Check for overload
                    available = state.get("available_hours", 0)
                    if state["used_hours"] > available:
                        state["is_overloaded"] = True
                        violations.append({
                            "date": date_str,
                            "type": "overload",
                            "message": f"Day is overloaded: {state['used_hours']}h used of {available}h available"
                        })
                    
                    day["state_json"] = state
        
        return list(days_map.values()), violations
    
    def compute_state_hash(self, days: List[Dict]) -> str:
        """
        Compute a hash of the calendar state for versioning.
        
        Args:
            days: List of calendar day dictionaries
        
        Returns:
            SHA256 hash of the state
        """
        # Sort days by date for consistent hashing
        sorted_days = sorted(days, key=lambda d: d.get("date", ""))
        
        # Create a stable JSON representation
        state_str = json.dumps(sorted_days, sort_keys=True, default=str)
        
        return hashlib.sha256(state_str.encode()).hexdigest()
    
    def diff_states(
        self,
        before: List[Dict],
        after: List[Dict]
    ) -> Dict:
        """
        Calculate the difference between two calendar states.
        
        Args:
            before: Calendar state before changes
            after: Calendar state after changes
        
        Returns:
            Diff object with changes
        """
        before_map = {d["date"]: d for d in before}
        after_map = {d["date"]: d for d in after}
        
        changes = []
        affected_dates = set()
        
        # Find added/modified days
        for date_str, after_day in after_map.items():
            before_day = before_map.get(date_str)
            
            if before_day is None:
                changes.append({
                    "type": "added",
                    "date": date_str,
                    "after": after_day
                })
                affected_dates.add(date_str)
            elif before_day != after_day:
                changes.append({
                    "type": "modified",
                    "date": date_str,
                    "before": before_day,
                    "after": after_day
                })
                affected_dates.add(date_str)
        
        # Find removed days
        for date_str in before_map:
            if date_str not in after_map:
                changes.append({
                    "type": "removed",
                    "date": date_str,
                    "before": before_map[date_str]
                })
                affected_dates.add(date_str)
        
        return {
            "changes": changes,
            "affected_dates": sorted(list(affected_dates)),
            "summary": f"{len(changes)} days changed"
        }
    
    def validate_cycle(self, cycle: Dict) -> List[str]:
        """
        Validate a cycle definition.
        
        Args:
            cycle: The cycle definition to validate
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        
        pattern = cycle.get("pattern", [])
        if not pattern:
            errors.append("Cycle must have at least one pattern block")
            return errors
        
        # Calculate total cycle length
        total_days = sum(block.get("duration", 0) for block in pattern)
        if total_days == 0:
            errors.append("Cycle must have non-zero duration")
            return errors
        
        # Validate anchor
        anchor_cycle_day = cycle.get("anchor_cycle_day", 0)
        if anchor_cycle_day < 1 or anchor_cycle_day > total_days:
            errors.append(f"anchor_cycle_day must be between 1 and {total_days}")
        
        # Validate anchor date
        anchor_date = cycle.get("anchor_date")
        if not anchor_date:
            errors.append("anchor_date is required")
        
        # Validate pattern blocks
        valid_work_types = {"work_day", "work_night", "off"}
        for i, block in enumerate(pattern):
            label = block.get("label")
            if label not in valid_work_types:
                errors.append(f"Block {i+1}: Invalid work type '{label}'")
            
            duration = block.get("duration", 0)
            if duration < 1:
                errors.append(f"Block {i+1}: Duration must be at least 1 day")
        
        return errors


def create_calendar_engine(user_id: str) -> CalendarEngine:
    """Factory function to create a CalendarEngine instance"""
    return CalendarEngine(user_id)

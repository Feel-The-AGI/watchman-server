"""
Watchman Mutation Engine
Handles proposal validation, binary constraints, escalation ladder, and alternatives generation.
Implements the core design law: Nothing mutates the calendar without explicit approval.
"""

from datetime import date, datetime
from typing import List, Dict, Optional, Tuple
from loguru import logger
import uuid

from app.models import (
    MutationStatus, ConstraintViolation, MutationAlternative,
    CommitmentType, WorkType
)


class MutationEngine:
    """
    The Mutation Engine enforces the approval-gated change control system.
    
    Escalation Ladder (when proposal fails binary constraints):
    1. Hard fail (no mutation) - Nothing applies
    2. Explain why (mandatory) - Clear, specific explanation
    3. Generate valid alternatives - Only alternatives that don't break binary rules
    4. Ask user what to do - User chooses, no auto-relaxation
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
    
    def validate_proposal(
        self,
        proposed_changes: Dict,
        current_state: List[Dict],
        constraints: List[Dict],
        commitments: List[Dict]
    ) -> Dict:
        """
        Validate a proposed mutation against binary constraints.
        
        Args:
            proposed_changes: The changes being proposed
            current_state: Current calendar state
            constraints: User's constraints
            commitments: User's current commitments
        
        Returns:
            Validation result with is_valid, violations, and alternatives
        """
        violations = []
        warnings = []
        
        intent = proposed_changes.get("intent", "")
        changes = proposed_changes.get("changes", [])
        
        # Validate each change against constraints
        for change in changes:
            change_violations = self._validate_change(
                change, constraints, commitments, current_state
            )
            violations.extend(change_violations)
        
        # Check concurrent commitment limits
        if intent in ["add_commitment", "propose_education_plan"]:
            limit_violation = self._check_concurrent_limits(
                proposed_changes, constraints, commitments
            )
            if limit_violation:
                violations.append(limit_violation)
        
        # Check work immutability
        work_violations = self._check_work_immutability(changes, constraints)
        violations.extend(work_violations)
        
        is_valid = len(violations) == 0
        
        result = {
            "is_valid": is_valid,
            "violations": violations,
            "warnings": warnings,
            "explanation": self._generate_explanation(is_valid, violations),
            "alternatives": []
        }
        
        # If not valid, generate alternatives
        if not is_valid:
            alternatives = self._generate_alternatives(
                proposed_changes, violations, constraints, commitments, current_state
            )
            result["alternatives"] = alternatives
        
        return result
    
    def _validate_change(
        self,
        change: Dict,
        constraints: List[Dict],
        commitments: List[Dict],
        current_state: List[Dict]
    ) -> List[Dict]:
        """Validate a single change against all constraints"""
        violations = []
        change_type = change.get("type", "")
        
        for constraint in constraints:
            if not constraint.get("is_active", True):
                continue
            
            rule = constraint.get("rule", {})
            rule_type = rule.get("type", "")
            
            # Check no_activity_on constraints
            if rule_type == "no_activity_on":
                violation = self._check_no_activity_on(change, rule, current_state)
                if violation:
                    violations.append({
                        "constraint_id": constraint.get("id"),
                        "constraint_name": constraint.get("name"),
                        "reason": violation,
                        "severity": "error"
                    })
            
            # Check immutable constraints
            elif rule_type == "immutable":
                if change_type in ["remove_work", "modify_work"]:
                    violations.append({
                        "constraint_id": constraint.get("id"),
                        "constraint_name": constraint.get("name"),
                        "reason": f"Cannot modify or remove {rule.get('scope', 'work')} - it is immutable",
                        "severity": "error"
                    })
        
        return violations
    
    def _check_no_activity_on(
        self,
        change: Dict,
        rule: Dict,
        current_state: List[Dict]
    ) -> Optional[str]:
        """Check if change violates no_activity_on constraint"""
        activity = rule.get("activity", "study")
        prohibited_work_types = rule.get("work_types", [])
        
        change_type = change.get("type", "")
        
        if change_type in ["add_commitment", "schedule_commitment"]:
            commitment_data = change.get("commitment", change.get("data", {}))
            commitment_type = commitment_data.get("type", "")
            
            if commitment_type in [activity, "study", "education"]:
                # Check if any affected dates fall on prohibited work types
                affected_dates = change.get("affected_dates", [])
                state_map = {d.get("date"): d for d in current_state}
                
                for date_str in affected_dates:
                    day = state_map.get(date_str)
                    if day:
                        work_type = day.get("work_type", "")
                        if work_type in prohibited_work_types:
                            return f"Cannot schedule {activity} on {work_type} days (affects {date_str})"
        
        return None
    
    def _check_concurrent_limits(
        self,
        proposed_changes: Dict,
        constraints: List[Dict],
        commitments: List[Dict]
    ) -> Optional[Dict]:
        """Check if adding commitment exceeds concurrent limits"""
        # Find max_concurrent constraint
        max_concurrent = 2  # Default
        constraint_info = None
        
        for constraint in constraints:
            rule = constraint.get("rule", {})
            if rule.get("type") == "max_concurrent":
                max_concurrent = rule.get("value", 2)
                constraint_info = constraint
                break
        
        # Count current active commitments in the scope
        scope = "education"  # Default scope
        if constraint_info:
            scope = constraint_info.get("rule", {}).get("scope", "education")
        
        active_count = sum(
            1 for c in commitments
            if c.get("status") == "active" and c.get("type") == scope
        )
        
        # Check if proposal adds a commitment
        changes = proposed_changes.get("changes", [])
        adds_commitment = any(
            c.get("type") == "add_commitment" and 
            c.get("commitment", {}).get("type") == scope
            for c in changes
        )
        
        if adds_commitment and active_count >= max_concurrent:
            return {
                "constraint_id": constraint_info.get("id") if constraint_info else None,
                "constraint_name": f"Maximum {max_concurrent} concurrent {scope} commitments",
                "reason": f"Already have {active_count} active {scope} commitments (max: {max_concurrent})",
                "severity": "error"
            }
        
        return None
    
    def _check_work_immutability(
        self,
        changes: List[Dict],
        constraints: List[Dict]
    ) -> List[Dict]:
        """Check if any changes attempt to modify immutable work"""
        violations = []
        
        # Find immutability constraint for work
        work_immutable = any(
            c.get("rule", {}).get("type") == "immutable" and
            c.get("rule", {}).get("scope") == "work" and
            c.get("is_active", True)
            for c in constraints
        )
        
        if work_immutable:
            for change in changes:
                change_type = change.get("type", "")
                if change_type in ["remove_work", "modify_work", "delete_work_days"]:
                    violations.append({
                        "constraint_id": None,
                        "constraint_name": "Work is immutable",
                        "reason": "Work schedule cannot be modified or removed",
                        "severity": "error"
                    })
        
        return violations
    
    def _generate_explanation(
        self,
        is_valid: bool,
        violations: List[Dict]
    ) -> str:
        """Generate human-readable explanation of validation result"""
        if is_valid:
            return "All changes are valid and can be applied."
        
        explanation_parts = ["This proposal cannot be applied because:"]
        
        for i, violation in enumerate(violations, 1):
            explanation_parts.append(f"{i}. {violation['reason']}")
        
        return "\n".join(explanation_parts)
    
    def _generate_alternatives(
        self,
        proposed_changes: Dict,
        violations: List[Dict],
        constraints: List[Dict],
        commitments: List[Dict],
        current_state: List[Dict]
    ) -> List[Dict]:
        """
        Generate valid alternatives when a proposal fails.
        Only generates alternatives that don't break binary rules.
        """
        alternatives = []
        intent = proposed_changes.get("intent", "")
        changes = proposed_changes.get("changes", [])
        
        # Check if the issue is concurrent limit
        has_concurrent_violation = any(
            "concurrent" in v.get("reason", "").lower() or "max" in v.get("constraint_name", "").lower()
            for v in violations
        )
        
        if has_concurrent_violation and intent in ["add_commitment", "propose_education_plan"]:
            # Alternative 1: Queue the commitment
            alternatives.append({
                "id": str(uuid.uuid4()),
                "description": "Queue this commitment to start after a current one ends",
                "type": "queue_commitment",
                "changes": self._create_queue_changes(changes),
                "is_valid": True
            })
            
            # Alternative 2: Replace an existing commitment
            active_commitments = [
                c for c in commitments
                if c.get("status") == "active" and c.get("type") == "education"
            ]
            
            for existing in active_commitments[:2]:  # Max 2 replace options
                alternatives.append({
                    "id": str(uuid.uuid4()),
                    "description": f"Replace '{existing.get('name')}' with the new commitment",
                    "type": "replace_commitment",
                    "changes": self._create_replace_changes(changes, existing),
                    "is_valid": True
                })
            
            # Alternative 3: Mark as pending
            alternatives.append({
                "id": str(uuid.uuid4()),
                "description": "Keep this commitment as pending with a reminder",
                "type": "mark_pending",
                "changes": self._create_pending_changes(changes),
                "is_valid": True
            })
        
        # Check if issue is schedule conflict (night shifts)
        has_schedule_conflict = any(
            "night" in v.get("reason", "").lower() or 
            "schedule" in v.get("reason", "").lower()
            for v in violations
        )
        
        if has_schedule_conflict:
            # Alternative: Schedule only on valid days
            alternatives.append({
                "id": str(uuid.uuid4()),
                "description": "Schedule only on off days and day shift evenings (skipping conflicting dates)",
                "type": "schedule_valid_only",
                "changes": self._create_valid_schedule_changes(changes, current_state, constraints),
                "is_valid": True
            })
        
        return alternatives
    
    def _create_queue_changes(self, original_changes: List[Dict]) -> List[Dict]:
        """Create changes that queue the commitment instead of activating it"""
        new_changes = []
        for change in original_changes:
            if change.get("type") == "add_commitment":
                new_change = change.copy()
                new_change["commitment"] = change.get("commitment", {}).copy()
                new_change["commitment"]["status"] = "queued"
                new_changes.append(new_change)
            else:
                new_changes.append(change)
        return new_changes
    
    def _create_replace_changes(
        self,
        original_changes: List[Dict],
        existing_commitment: Dict
    ) -> List[Dict]:
        """Create changes that replace an existing commitment"""
        new_changes = [
            {
                "type": "update_commitment",
                "commitment_id": existing_commitment.get("id"),
                "updates": {"status": "paused"}
            }
        ]
        new_changes.extend(original_changes)
        return new_changes
    
    def _create_pending_changes(self, original_changes: List[Dict]) -> List[Dict]:
        """Create changes that mark commitment as pending"""
        new_changes = []
        for change in original_changes:
            if change.get("type") == "add_commitment":
                new_change = change.copy()
                new_change["commitment"] = change.get("commitment", {}).copy()
                new_change["commitment"]["status"] = "queued"
                new_change["commitment"]["notes"] = "Pending - awaiting slot"
                new_changes.append(new_change)
        return new_changes
    
    def _create_valid_schedule_changes(
        self,
        original_changes: List[Dict],
        current_state: List[Dict],
        constraints: List[Dict]
    ) -> List[Dict]:
        """Create changes that only schedule on valid days"""
        # Find prohibited work types
        prohibited = set()
        for constraint in constraints:
            rule = constraint.get("rule", {})
            if rule.get("type") == "no_activity_on":
                prohibited.update(rule.get("work_types", []))
        
        # Filter affected dates
        state_map = {d.get("date"): d for d in current_state}
        
        new_changes = []
        for change in original_changes:
            new_change = change.copy()
            
            if "affected_dates" in change:
                valid_dates = [
                    d for d in change["affected_dates"]
                    if state_map.get(d, {}).get("work_type") not in prohibited
                ]
                new_change["affected_dates"] = valid_dates
            
            new_changes.append(new_change)
        
        return new_changes
    
    def apply_mutation(
        self,
        mutation: Dict,
        current_state: List[Dict]
    ) -> Tuple[List[Dict], str]:
        """
        Apply an approved mutation to the calendar state.
        
        Args:
            mutation: The approved mutation to apply
            current_state: Current calendar state
        
        Returns:
            Tuple of (new_state, state_hash)
        """
        changes = mutation.get("proposed_diff", {}).get("changes", [])
        new_state = [d.copy() for d in current_state]  # Deep copy
        state_map = {d.get("date"): d for d in new_state}
        
        for change in changes:
            change_type = change.get("type", "")
            
            if change_type == "add_commitment":
                commitment = change.get("commitment", {})
                affected_dates = change.get("affected_dates", [])
                
                for date_str in affected_dates:
                    if date_str in state_map:
                        day = state_map[date_str]
                        state_json = day.get("state_json", {})
                        commitments_list = state_json.get("commitments", [])
                        
                        commitments_list.append({
                            "commitment_id": commitment.get("id"),
                            "name": commitment.get("name"),
                            "type": commitment.get("type"),
                            "hours": commitment.get("duration_hours", 2),
                            "is_preview": False
                        })
                        
                        state_json["commitments"] = commitments_list
                        state_json["used_hours"] = sum(
                            c.get("hours", 0) for c in commitments_list
                        )
                        
                        day["state_json"] = state_json
            
            elif change_type == "remove_commitment":
                commitment_id = change.get("commitment_id")
                
                for day in new_state:
                    state_json = day.get("state_json", {})
                    commitments_list = state_json.get("commitments", [])
                    
                    state_json["commitments"] = [
                        c for c in commitments_list
                        if c.get("commitment_id") != commitment_id
                    ]
                    
                    state_json["used_hours"] = sum(
                        c.get("hours", 0) for c in state_json["commitments"]
                    )
                    
                    day["state_json"] = state_json
            
            elif change_type == "add_leave":
                leave_data = change.get("leave", {})
                start_date = leave_data.get("start_date")
                end_date = leave_data.get("end_date")
                
                for day in new_state:
                    date_str = day.get("date")
                    if start_date <= date_str <= end_date:
                        state_json = day.get("state_json", {})
                        state_json["is_leave"] = True
                        state_json["available_hours"] = 16.0
                        if "leave" not in state_json.get("tags", []):
                            state_json.setdefault("tags", []).append("leave")
                        day["state_json"] = state_json
        
        # Compute new state hash
        import hashlib
        import json
        state_str = json.dumps(new_state, sort_keys=True, default=str)
        state_hash = hashlib.sha256(state_str.encode()).hexdigest()
        
        return new_state, state_hash
    
    def create_mutation_record(
        self,
        intent: str,
        proposed_diff: Dict,
        explanation: str,
        violations: List[Dict] = None,
        alternatives: List[Dict] = None,
        source_text: str = None,
        triggered_by: str = "user"
    ) -> Dict:
        """Create a mutation record for storage"""
        return {
            "user_id": self.user_id,
            "status": "proposed",
            "intent": intent,
            "proposed_diff": proposed_diff,
            "explanation": explanation,
            "violations": violations,
            "alternatives": alternatives,
            "source_text": source_text,
            "triggered_by": triggered_by,
            "proposed_at": datetime.utcnow().isoformat()
        }


def create_mutation_engine(user_id: str) -> MutationEngine:
    """Factory function to create a MutationEngine instance"""
    return MutationEngine(user_id)

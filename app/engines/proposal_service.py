"""
Watchman Proposal Service
LLM integration for parsing unstructured input to structured JSON.
The LLM only parses - it never touches calendar logic directly.

Uses Google Gemini 2.5 Pro with structured outputs (Pydantic schema).
"""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from loguru import logger
import json
import re

import base64
from app.config import get_settings


# ============================================
# Pydantic models for structured Gemini output
# ============================================

class CommitmentConstraints(BaseModel):
    """Constraints for a commitment"""
    study_on: Optional[List[str]] = Field(
        default=None,
        description="When this commitment can be scheduled: 'off', 'work_day_evening'"
    )
    exclude: Optional[List[str]] = Field(
        default=None,
        description="Work types to exclude: 'work_day', 'work_night'"
    )
    frequency: Optional[str] = Field(
        default=None,
        description="Frequency: 'weekly', 'daily', 'bi-weekly', 'monthly'"
    )
    duration_hours: Optional[float] = Field(
        default=None,
        description="Duration in hours per session"
    )


class CommitmentChange(BaseModel):
    """A commitment to add or modify"""
    name: str = Field(description="Name of the commitment")
    type: str = Field(description="Type: 'education', 'personal', 'study', 'leave', 'work'")
    priority: Optional[int] = Field(default=None, description="Priority level 1-5")
    constraints_json: Optional[CommitmentConstraints] = Field(default=None)
    start_date: Optional[str] = Field(default=None, description="Start date in ISO format YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="End date in ISO format YYYY-MM-DD")


class LeaveChange(BaseModel):
    """A leave period to add"""
    name: str = Field(default="Leave", description="Name of the leave period")
    start_date: str = Field(description="Start date in ISO format YYYY-MM-DD")
    end_date: str = Field(description="End date in ISO format YYYY-MM-DD")
    reason: Optional[str] = Field(default=None, description="Reason for leave")


class MutationChange(BaseModel):
    """A single change in a mutation"""
    type: str = Field(
        description="Type of change: 'add_commitment', 'update_commitment', 'remove_commitment', 'add_leave', 'schedule_commitment'"
    )
    commitment: Optional[CommitmentChange] = Field(default=None)
    leave: Optional[LeaveChange] = Field(default=None)
    affected_dates: Optional[List[str]] = Field(
        default=None,
        description="List of affected dates in ISO format"
    )


class CalendarMutation(BaseModel):
    """The structured mutation output from LLM parsing"""
    intent: str = Field(
        description="Intent: 'add_commitment', 'update_commitment', 'remove_commitment', 'add_leave', 'update_schedule', 'propose_education_plan'"
    )
    scope_start: Optional[str] = Field(
        default=None,
        description="Scope start date in ISO format YYYY-MM-DD"
    )
    scope_end: Optional[str] = Field(
        default=None,
        description="Scope end date in ISO format YYYY-MM-DD"
    )
    changes: List[MutationChange] = Field(
        description="List of changes to apply"
    )
    explanation: str = Field(
        description="Human-readable explanation of what was understood from the input"
    )
    confidence: float = Field(
        default=0.8,
        description="Confidence level 0.0-1.0 in the parsing accuracy"
    )


class ProposalService:
    """
    The Proposal Service translates messy human input into structured mutations.
    
    Uses Gemini 2.5 Pro with native structured outputs (Pydantic schema).
    
    LLM Boundary (non-negotiable):
    - LLM can: Parse inputs, Generate Proposed Mutations, Write explanations
    - LLM cannot: Commit changes, Recalculate state, Auto-correct silently
    
    If output ≠ schema → hard fail.
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.settings = get_settings()
        self._client = None
    
    def _get_gemini_client(self):
        """Lazy initialization of Gemini client using new google-genai SDK"""
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client
    
    async def parse_input(
        self,
        text: str,
        context: Optional[Dict] = None
    ) -> Dict:
        """
        Parse unstructured text input and generate a structured proposal.
        Uses Gemini 2.5 Pro with native structured output (Pydantic schema).
        
        Args:
            text: The raw input text (email, WhatsApp message, etc.)
            context: Optional context about user's current state
        
        Returns:
            Parsed proposal with structured changes
        """
        if not self.settings.gemini_api_key:
            logger.warning("Gemini API key not configured, using fallback parser")
            return await self._fallback_parse(text, context)
        
        try:
            client = self._get_gemini_client()
            
            # Build the prompt
            prompt = self._build_prompt(text, context)
            
            # Call Gemini 2.5 Pro with structured output using Pydantic schema
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": CalendarMutation.model_json_schema(),
                }
            )
            
            # Parse and validate using Pydantic
            try:
                mutation = CalendarMutation.model_validate_json(response.text)
                result = mutation.model_dump()
            except Exception as e:
                logger.warning(f"Pydantic validation failed: {e}")
                # Try manual JSON parse as fallback
                result = self._parse_response(response.text)
                if not self._validate_schema(result):
                    return {
                        "success": False,
                        "error": "Failed to parse input into valid structure",
                        "raw_response": response.text
                    }
            
            return {
                "success": True,
                "parsed": result,
                "confidence": result.get("confidence", 0.8)
            }
            
        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}")
            return await self._fallback_parse(text, context)
    
    def _build_prompt(self, text: str, context: Optional[Dict]) -> str:
        """Build the prompt for LLM parsing - simplified since we use native structured outputs"""
        context_str = ""
        if context:
            context_str = f"""
Current User Context:
- Active commitments: {json.dumps(context.get('active_commitments', []), indent=2)}
- Work rotation: {context.get('rotation_summary', 'Not specified')}
- Active constraints: {json.dumps(context.get('constraints', []), indent=2)}
- Max concurrent education commitments: {context.get('max_concurrent_commitments', 2)}
"""
        
        return f"""You are a calendar mutation parser for Watchman, a deterministic life-state simulator.

Your ONLY job is to parse the user's input and convert it into a structured calendar mutation.

RULES:
1. Parse the input carefully and extract dates, commitment types, and scheduling details
2. Do NOT make assumptions about unspecified details - leave optional fields null
3. If something is unclear, set confidence lower and explain in the explanation field
4. Never auto-correct or guess dates/times that aren't explicitly mentioned
5. For education commitments, default to excluding night shifts unless specified otherwise
6. Valid intent types: add_commitment, update_commitment, remove_commitment, add_leave, update_schedule, propose_education_plan
7. Valid commitment types: education, personal, study, leave, work
8. Dates must be in ISO format: YYYY-MM-DD
{context_str}

USER INPUT:
\"\"\"{text}\"\"\"

Parse this input and generate a structured mutation."""
    
    def _parse_response(self, response_text: str) -> Dict:
        """Parse the LLM response text into structured data"""
        # Try to extract JSON from the response
        text = response_text.strip()
        
        # Remove markdown code blocks if present
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        
        text = text.strip()
        
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            
            # Try to find JSON in the response
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
            
            return {"error": "Failed to parse response", "raw": text}
    
    def _validate_schema(self, parsed: Dict) -> bool:
        """Validate that the parsed result matches the expected schema"""
        if "error" in parsed:
            return False
        
        required_fields = ["intent", "changes", "explanation"]
        
        for field in required_fields:
            if field not in parsed:
                logger.warning(f"Missing required field: {field}")
                return False
        
        valid_intents = [
            "add_commitment", "update_commitment", "remove_commitment",
            "add_leave", "update_schedule", "propose_education_plan"
        ]
        
        if parsed.get("intent") not in valid_intents:
            logger.warning(f"Invalid intent: {parsed.get('intent')}")
            return False
        
        if not isinstance(parsed.get("changes"), list):
            logger.warning("changes must be a list")
            return False
        
        return True
    
    async def _fallback_parse(self, text: str, context: Optional[Dict]) -> Dict:
        """Fallback parser when LLM is not available"""
        text_lower = text.lower()
        
        # Simple pattern matching for common scenarios
        result = {
            "success": True,
            "parsed": {
                "intent": "add_commitment",
                "changes": [],
                "explanation": "Parsed using fallback parser (LLM not available)",
                "confidence": 0.5
            }
        }
        
        # Detect leave/vacation
        if any(word in text_lower for word in ["leave", "vacation", "holiday", "off"]):
            result["parsed"]["intent"] = "add_leave"
            
            # Try to extract dates
            dates = self._extract_dates(text)
            if len(dates) >= 2:
                result["parsed"]["changes"] = [{
                    "type": "add_leave",
                    "leave": {
                        "name": "Leave",
                        "start_date": dates[0],
                        "end_date": dates[1]
                    }
                }]
        
        # Detect education/course/certificate
        elif any(word in text_lower for word in ["course", "certificate", "diploma", "class", "study"]):
            result["parsed"]["intent"] = "add_commitment"
            
            # Extract course name if possible
            name = self._extract_name(text)
            
            result["parsed"]["changes"] = [{
                "type": "add_commitment",
                "commitment": {
                    "name": name or "New Course",
                    "type": "education",
                    "constraints_json": {
                        "study_on": ["off", "work_day_evening"],
                        "exclude": ["work_night"],
                        "frequency": "weekly",
                        "duration_hours": 2
                    }
                }
            }]
        
        # Default: general change request
        else:
            result["parsed"]["explanation"] = "Could not determine intent from input"
            result["parsed"]["confidence"] = 0.3
        
        return result
    
    def _extract_dates(self, text: str) -> List[str]:
        """Extract date strings from text"""
        dates = []
        
        # ISO format: YYYY-MM-DD
        iso_pattern = r'\d{4}-\d{2}-\d{2}'
        dates.extend(re.findall(iso_pattern, text))
        
        # Month Day format: January 15, March 3
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]
        for i, month in enumerate(month_names, 1):
            pattern = rf'{month}\s+(\d{{1,2}})'
            matches = re.findall(pattern, text.lower())
            for day in matches:
                # Assume 2026 if not specified
                dates.append(f"2026-{i:02d}-{int(day):02d}")
        
        return dates
    
    def _extract_name(self, text: str) -> Optional[str]:
        """Try to extract a name/title from text"""
        # Look for quoted text
        quoted = re.findall(r'"([^"]+)"', text)
        if quoted:
            return quoted[0]
        
        quoted = re.findall(r"'([^']+)'", text)
        if quoted:
            return quoted[0]
        
        # Look for "Certificate in X" or "Diploma in X"
        cert_match = re.search(r'(certificate|diploma)\s+in\s+([^,.\n]+)', text, re.I)
        if cert_match:
            return f"{cert_match.group(1).title()} in {cert_match.group(2).strip()}"
        
        return None
    
    def generate_explanation(
        self,
        mutation: Dict,
        violations: List[Dict] = None,
        alternatives: List[Dict] = None
    ) -> str:
        """
        Generate a human-readable explanation for a mutation.
        
        Args:
            mutation: The mutation to explain
            violations: Any constraint violations
            alternatives: Alternative proposals if any
        
        Returns:
            Human-readable explanation string
        """
        parts = []
        
        intent = mutation.get("intent", "unknown")
        changes = mutation.get("changes", [])
        
        # Describe the intent
        intent_descriptions = {
            "add_commitment": "Add a new commitment to your calendar",
            "update_commitment": "Update an existing commitment",
            "remove_commitment": "Remove a commitment from your calendar",
            "add_leave": "Add a leave period",
            "update_schedule": "Update your schedule",
            "propose_education_plan": "Propose an education plan"
        }
        parts.append(f"**Intent:** {intent_descriptions.get(intent, intent)}")
        
        # Describe changes
        if changes:
            parts.append("\n**Changes:**")
            for i, change in enumerate(changes, 1):
                change_type = change.get("type", "unknown")
                
                if change_type == "add_commitment":
                    commitment = change.get("commitment", {})
                    parts.append(f"  {i}. Add '{commitment.get('name', 'New commitment')}' ({commitment.get('type', 'unknown')})")
                    
                    constraints = commitment.get("constraints_json", {})
                    if constraints.get("study_on"):
                        parts.append(f"     - Scheduled on: {', '.join(constraints['study_on'])}")
                    if constraints.get("exclude"):
                        parts.append(f"     - Excluded from: {', '.join(constraints['exclude'])}")
                
                elif change_type == "add_leave":
                    leave = change.get("leave", {})
                    parts.append(f"  {i}. Add leave from {leave.get('start_date')} to {leave.get('end_date')}")
        
        # Describe violations if any
        if violations:
            parts.append("\n**Constraint Violations:**")
            for v in violations:
                parts.append(f"  - {v.get('reason', 'Unknown violation')}")
        
        # Describe alternatives if any
        if alternatives:
            parts.append("\n**Alternatives Available:**")
            for alt in alternatives:
                parts.append(f"  - {alt.get('description', 'Alternative option')}")
        
        return "\n".join(parts)
    
    def create_preview(
        self,
        parsed_input: Dict,
        validation_result: Dict,
        stats_impact: Optional[Dict] = None
    ) -> Dict:
        """
        Create a preview of what a proposal would do.
        
        Args:
            parsed_input: The parsed input from LLM
            validation_result: Result from mutation engine validation
            stats_impact: Optional statistics impact calculation
        
        Returns:
            Preview dictionary for UI display
        """
        is_valid = validation_result.get("is_valid", False)
        
        preview = {
            "is_valid": is_valid,
            "mutation": parsed_input.get("parsed") if parsed_input.get("success") else None,
            "violations": validation_result.get("violations", []),
            "alternatives": validation_result.get("alternatives", []),
            "explanation": self.generate_explanation(
                parsed_input.get("parsed", {}),
                validation_result.get("violations"),
                validation_result.get("alternatives")
            ),
            "affected_dates": [],
            "stats_impact": stats_impact
        }
        
        # Collect affected dates from changes
        if preview["mutation"]:
            for change in preview["mutation"].get("changes", []):
                dates = change.get("affected_dates", [])
                preview["affected_dates"].extend(dates)
        
        preview["affected_dates"] = list(set(preview["affected_dates"]))
        
        return preview


    async def parse_pdf(
        self,
        pdf_bytes: bytes,
        context: Optional[Dict] = None
    ) -> Dict:
        """
        Parse PDF content using Gemini's multimodal capabilities.
        
        Args:
            pdf_bytes: Raw PDF file bytes
            context: Optional context about user's current state
        
        Returns:
            Parsed proposal with structured changes
        """
        if not self.settings.gemini_api_key:
            logger.warning("Gemini API key not configured")
            return {
                "success": False,
                "error": "PDF parsing requires Gemini API key"
            }
        
        try:
            logger.info(f"Parsing PDF ({len(pdf_bytes)} bytes) via Gemini")
            client = self._get_gemini_client()
            
            # Build context string
            context_str = ""
            if context:
                context_str = f"""
Current User Context:
- Active commitments: {json.dumps(context.get('active_commitments', []), indent=2)}
- Work rotation: {context.get('rotation_summary', 'Not specified')}
"""
            
            # Create prompt for PDF parsing
            prompt = f"""You are a calendar mutation parser for Watchman.

Extract any scheduling information from this PDF and convert it into structured calendar changes.
Look for:
- Course schedules, exam dates, class times
- Work shifts or rotation schedules  
- Appointment dates and times
- Leave periods or holidays
- Any commitment with dates

{context_str}

If the PDF doesn't contain scheduling information, explain what you found instead."""

            # Encode PDF as base64 for Gemini
            pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
            
            # Call Gemini with PDF content
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt},
                            {
                                "inline_data": {
                                    "mime_type": "application/pdf",
                                    "data": pdf_base64
                                }
                            }
                        ]
                    }
                ],
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": CalendarMutation.model_json_schema(),
                }
            )
            
            # Parse response
            try:
                mutation = CalendarMutation.model_validate_json(response.text)
                result = mutation.model_dump()
            except Exception as e:
                logger.warning(f"Pydantic validation failed: {e}")
                result = self._parse_response(response.text)
                if not self._validate_schema(result):
                    return {
                        "success": False,
                        "error": "Couldn't extract structured schedule from PDF",
                        "raw_response": response.text
                    }
            
            logger.info(f"Successfully parsed PDF - found {len(result.get('changes', []))} changes")
            return {
                "success": True,
                "parsed": result,
                "confidence": result.get("confidence", 0.8)
            }
            
        except Exception as e:
            logger.error(f"PDF parsing error: {e}")
            return {
                "success": False,
                "error": f"Failed to parse PDF: {str(e)}"
            }


def create_proposal_service(user_id: str) -> ProposalService:
    """Factory function to create a ProposalService instance"""
    return ProposalService(user_id)

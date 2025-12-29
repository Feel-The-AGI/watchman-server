"""
Chat Service
Handles conversation with user and coordinates with Gemini for command execution via tool calling
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, date, timedelta
from uuid import uuid4
from loguru import logger
import json
import os

from google import genai
from google.genai import types

from app.database import Database
from app.engines.master_settings_service import MasterSettingsService
from app.engines.command_executor import CommandExecutor


# Define tools (function declarations) for Gemini
WATCHMAN_TOOLS = [
    {
        "name": "override_days",
        "description": """Bulk update calendar days to a specific work type. Use this to:
- Correct past calendar entries that are wrong
- Convert night shifts to day shifts (or vice versa) while keeping off days unchanged
- Set a range of dates to day shifts, night shifts, off, or blank (untracked)
- Mark dates as 'blank' when the user's schedule was chaotic/unknown during that period
IMPORTANT: When user says 'set working days to X' or 'change shifts to X', set preserve_off_days=true to keep off days unchanged.
Only set preserve_off_days=false when user explicitly wants ALL days changed (including off days).
Use 'blank' for dates before the user's rotation became stable/consistent.""",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format (e.g., '2025-10-16')"
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format (e.g., '2025-12-14')"
                },
                "work_type": {
                    "type": "string",
                    "enum": ["work_day", "work_night", "off", "blank"],
                    "description": "The work type to set. work_day=day shift, work_night=night shift, off=rest day, blank=untracked/unknown"
                },
                "preserve_off_days": {
                    "type": "boolean",
                    "description": "If true, keeps existing off/rest days unchanged. Default true for 'working days' requests."
                }
            },
            "required": ["start_date", "end_date", "work_type"]
        }
    },
    {
        "name": "update_cycle",
        "description": """Update the work rotation pattern or regenerate the calendar. Use this to:
- Set up a new rotation pattern (e.g., 5 days, 5 nights, 5 off)
- Change the anchor date (which date corresponds to which cycle day)
- Regenerate calendar from the current pattern
- Handle "off by one day" scenarios by adjusting anchor_cycle_day

IMPORTANT CONCEPT - ROTATION ANCHOR vs EMPLOYMENT START:
- The anchor_date is when the user's STABLE ROTATION began, not their employment start date
- Many workers have chaotic/random schedules during onboarding before getting a stable rotation
- The anchor represents when the repeating pattern became consistent
- Days BEFORE the anchor can be marked as 'blank' (untracked) if the schedule was unstable""",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the rotation (e.g., 'My Rotation')"
                },
                "pattern": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "enum": ["work_day", "work_night", "off"]
                            },
                            "duration": {
                                "type": "integer",
                                "description": "Number of consecutive days"
                            }
                        },
                        "required": ["label", "duration"]
                    },
                    "description": "The rotation pattern as blocks (e.g., 5 day shifts, 5 night shifts, 5 off)"
                },
                "anchor_date": {
                    "type": "string",
                    "description": "A known date in YYYY-MM-DD format - this is when the stable rotation started"
                },
                "anchor_cycle_day": {
                    "type": "integer",
                    "description": "Which day of the cycle the anchor_date falls on (1-based). Adjusting this shifts the entire calendar."
                },
                "shift_by_days": {
                    "type": "integer",
                    "description": "Alternative way to shift the rotation: positive = forward, negative = backward. Use this for 'off by one day' fixes."
                }
            },
            "required": []
        }
    },
    {
        "name": "copy_incident",
        "description": """Copy an incident report from one date to another. Use when user says things like 'copy the incident from the 23rd to the 22nd'.""",
        "parameters": {
            "type": "object",
            "properties": {
                "source_date": {
                    "type": "string",
                    "description": "Date to copy FROM in YYYY-MM-DD format"
                },
                "target_date": {
                    "type": "string",
                    "description": "Date to copy TO in YYYY-MM-DD format"
                }
            },
            "required": ["source_date", "target_date"]
        }
    },
    {
        "name": "add_leave",
        "description": "Block out leave/vacation dates on the calendar",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the leave (e.g., 'Annual Leave', 'Vacation')"
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format"
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format"
                },
                "type": {
                    "type": "string",
                    "enum": ["annual", "sick", "personal", "other"],
                    "description": "Type of leave"
                }
            },
            "required": ["name", "start_date", "end_date"]
        }
    },
    {
        "name": "add_commitment",
        "description": "Add a recurring commitment like a course, class, or regular appointment",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the commitment (e.g., 'Diploma in Survey')"
                },
                "type": {
                    "type": "string",
                    "enum": ["education", "personal", "work", "other"],
                    "description": "Type of commitment"
                },
                "days_of_week": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Days when this occurs (e.g., ['monday', 'wednesday'])"
                },
                "time_start": {
                    "type": "string",
                    "description": "Start time in HH:MM format"
                },
                "time_end": {
                    "type": "string",
                    "description": "End time in HH:MM format"
                },
                "start_date": {
                    "type": "string",
                    "description": "When the commitment starts (YYYY-MM-DD)"
                },
                "end_date": {
                    "type": "string",
                    "description": "When the commitment ends (YYYY-MM-DD)"
                }
            },
            "required": ["name", "type"]
        }
    },
    {
        "name": "create_daily_log",
        "description": """Create a daily note/log entry for a specific date. Use this when:
- User wants to document something that happened at work
- User wants to add notes about their day
- User mentions they want to log or record something
- User talks about their actual hours worked

Examples:
- "Log that I worked 10 hours today with 2 hours overtime"
- "Note: Had a good training session with new team member"
- "Record that I did extra safety checks this morning"
""",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date for the log in YYYY-MM-DD format. Use today's date if not specified."
                },
                "note": {
                    "type": "string",
                    "description": "The note/log content. Write this as a clear, professional note."
                },
                "actual_hours": {
                    "type": "number",
                    "description": "Actual hours worked (optional, e.g., 8, 10, 12)"
                },
                "overtime_hours": {
                    "type": "number",
                    "description": "Overtime hours worked (optional, e.g., 2, 4)"
                }
            },
            "required": ["date", "note"]
        }
    },
    {
        "name": "create_incident",
        "description": """Log a workplace incident or issue. Use this when:
- User reports a safety issue, injury, or hazard
- User mentions harassment, discrimination, or hostile behavior
- User had a health issue, got sick, or had medical problems at work
- User reports overtime violations or being forced to work extra
- User mentions equipment failures or broken tools
- User describes policy violations or unfair treatment
- User talks about pay issues, scheduling problems, or workload concerns
- User wants to formally document any workplace problem

INCIDENT TYPES (choose the most appropriate):
- overtime: Forced overtime, unpaid overtime, excessive hours
- safety: Safety hazards, unsafe conditions, safety violations
- equipment: Equipment failure, broken tools, malfunctioning machinery
- harassment: Verbal abuse, bullying, inappropriate behavior
- injury: Physical injury, accident, hurt at work
- policy_violation: Rules broken, unfair practices, procedural issues
- health: Got sick, medical issue, illness, feeling unwell, health problem
- discrimination: Unfair treatment based on race, gender, age, etc.
- workload: Excessive workload, unreasonable demands, understaffing
- compensation: Pay issues, unpaid work, wage theft, denied benefits
- scheduling: Shift conflicts, unfair scheduling, roster problems
- communication: Lack of information, miscommunication, withheld info
- retaliation: Punishment for reporting issues, whistleblower retaliation
- environment: Hostile work environment, poor conditions, cleanliness
- other: Anything else not covered above

SEVERITY LEVELS:
- low: Minor issue, inconvenience, no immediate harm
- medium: Moderate concern, needs attention soon
- high: Serious issue, needs urgent attention
- critical: Emergency, immediate danger, requires instant action
""",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date of the incident in YYYY-MM-DD format. Use today if not specified."
                },
                "type": {
                    "type": "string",
                    "enum": ["overtime", "safety", "equipment", "harassment", "injury", "policy_violation", "health", "discrimination", "workload", "compensation", "scheduling", "communication", "retaliation", "environment", "other"],
                    "description": "Type of incident - choose the most appropriate category"
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Severity level of the incident"
                },
                "title": {
                    "type": "string",
                    "description": "Brief title summarizing the incident (e.g., 'Forced to work 4 hours overtime')"
                },
                "description": {
                    "type": "string",
                    "description": "Detailed description of what happened. Be specific and factual."
                },
                "reported_to": {
                    "type": "string",
                    "description": "Who was this reported to (optional, e.g., 'Supervisor John', 'HR', 'Safety Officer')"
                },
                "witnesses": {
                    "type": "string",
                    "description": "Names of any witnesses (optional)"
                }
            },
            "required": ["date", "type", "severity", "title", "description"]
        }
    },
    {
        "name": "undo",
        "description": "Undo the last change made to the calendar or settings",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]


# System prompt - comprehensive, context-aware, with safety guardrails
SYSTEM_PROMPT = """You are Watchman, an intelligent calendar assistant designed specifically for shift workers.
You understand the unique challenges of rotating schedules, FIFO work, healthcare shifts, mining operations,
and other industries where work patterns don't follow a standard 9-5 schedule.

=== CURRENT DATE & TIME ===
Today: {current_date}

=== USER'S MASTER SETTINGS ===
{master_settings}

=== USER'S CALENDAR (Recent & Upcoming Days) ===
Legend: D=Day Shift, N=Night Shift, O=Off, B=Blank/Untracked
{calendar_snapshot}

=== USER'S RECENT LOGS & INCIDENTS ===
{logs_and_incidents}

=== YOUR CAPABILITIES (TOOLS) ===
You have direct access to modify the user's calendar and records. Use these tools - your words alone don't change anything!

CALENDAR MANAGEMENT:
• override_days - Change date ranges to: day shifts, night shifts, off days, or blank (untracked)
• update_cycle - Set/modify rotation pattern, change anchor date, or shift calendar by N days
• add_leave - Block out vacation/leave dates
• add_commitment - Add recurring events (classes, appointments)
• copy_incident - Copy an incident report from one date to another

DOCUMENTATION:
• create_daily_log - Add notes, record actual hours worked, overtime
• create_incident - Log workplace issues (safety, harassment, overtime, health, equipment, etc.)

HISTORY:
• undo - Revert the last change

=== CRITICAL CONCEPTS ===

1. ROTATION ANCHOR vs EMPLOYMENT START:
   - The "anchor date" is when the user's STABLE, REPEATING rotation began
   - This is NOT necessarily when they started employment
   - Many workers have chaotic/random schedules during onboarding before getting assigned a stable shift
   - If user says their schedule was messy before a certain date, mark those days as 'blank'

2. "OFF BY ONE DAY" FIXES:
   - If user says "my calendar is one day off" or "shifted by a day"
   - Use update_cycle with shift_by_days parameter (+1 or -1)
   - This is a RE-ANCHOR operation, not editing history

3. UNDERSTANDING SHIFT PATTERNS:
   - "5/5/5" typically means: 5 day shifts, 5 night shifts, 5 off days
   - "10 on 5 off" means: 10 working days, 5 off days
   - Users may say "I started Day 4" meaning their anchor date falls on cycle day 4
   - Parse natural language descriptions carefully

4. BLANK/UNTRACKED DAYS:
   - Use work_type="blank" for days where the schedule was unknown or chaotic
   - Perfect for: onboarding periods, transition periods, before rotation became stable
   - Blank days show as untracked - no wrong data is better than wrong data

=== SAFETY & APPROVAL GUARDRAILS ===

ALWAYS ASK FOR CONFIRMATION before:
• Deleting or overwriting large date ranges (more than 30 days)
• Changing the core rotation pattern
• Operations that seem to conflict with what user previously set up

NEVER:
• Make assumptions about dates without confirming with user
• Execute destructive operations without explanation
• Ignore user's explicit constraints or preferences

WHEN UNCERTAIN:
• Ask clarifying questions
• Explain what you're about to do and why
• Offer alternatives when the request is ambiguous

=== INCIDENT TYPES ===
Choose the most appropriate type:
• overtime: Forced/unpaid overtime, excessive hours
• safety: Hazards, unsafe conditions, safety violations
• equipment: Equipment failure, broken tools, machinery issues
• harassment: Verbal abuse, bullying, inappropriate behavior
• injury: Physical injury, accident at work
• policy_violation: Rules broken, unfair practices
• health: Illness, feeling unwell, medical issues at work
• discrimination: Unfair treatment based on protected characteristics
• workload: Excessive demands, understaffing
• compensation: Pay issues, wage theft, denied benefits
• scheduling: Shift conflicts, unfair scheduling
• communication: Miscommunication, withheld information
• retaliation: Punishment for reporting issues
• environment: Hostile conditions, poor workplace environment
• other: Anything else

SEVERITY LEVELS:
• low: Minor inconvenience, no immediate harm
• medium: Moderate concern, needs attention soon
• high: Serious issue, needs urgent attention
• critical: Emergency, immediate danger

=== INTERACTION STYLE ===

1. Be conversational but efficient - shift workers are busy
2. When user describes their schedule naturally, parse it and set it up
3. Acknowledge what you understood before executing
4. After making changes, briefly confirm what was done
5. If user references past incidents/logs, you can see them in the context above
6. Be empathetic about workplace issues - these are real problems affecting real people

=== EXAMPLES ===

UNDERSTANDING NATURAL LANGUAGE SCHEDULES:
User: "I work 5 days, then 5 nights, then 5 off. Today is my second night shift."
→ Pattern: [{{label: "work_day", duration: 5}}, {{label: "work_night", duration: 5}}, {{label: "off", duration: 5}}]
→ Anchor: {current_date} is cycle day 7 (5 days + 2 nights = day 7 of 15-day cycle)
→ Call update_cycle with this information

FIXING "OFF BY ONE DAY":
User: "My calendar is showing tomorrow as night shift but I'm actually on day shift"
→ This means the cycle is shifted by 1 day
→ Call update_cycle with shift_by_days=-1 (or +1 depending on direction)

HANDLING MESSY HISTORY:
User: "I started working August 1st but my actual rotation only began December 1st. Before that was random."
→ Set anchor_date to December 1st
→ Optionally mark Aug 1 - Nov 30 as 'blank' using override_days

COPYING INCIDENTS:
User: "Copy the incident from the 23rd to the 22nd"
→ Call copy_incident(source_date="2025-12-23", target_date="2025-12-22")

INCIDENT LOGGING:
User: "My supervisor yelled at me in front of everyone today for being 2 minutes late"
→ Call create_incident(
    date="{current_date}",
    type="harassment",
    severity="medium",
    title="Public verbal reprimand for minor lateness",
    description="Supervisor publicly reprimanded employee in front of coworkers for being 2 minutes late. This constitutes inappropriate behavior and public humiliation."
  )

Remember: You're here to help shift workers take control of their chaotic schedules. Be their ally.
"""


class ChatService:
    """Service for handling chat with Gemini using tool calling"""

    def __init__(self, db: Database, user_id: str):
        self.db = db
        self.user_id = user_id
        self.settings_service = MasterSettingsService(db)
        self.command_executor = CommandExecutor(db, user_id)

        # Initialize Gemini client
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set")

        self.client = genai.Client(api_key=api_key)
        self.model = "gemini-2.5-pro"

    async def _get_calendar_snapshot(self, days_back: int = 30, days_forward: int = 60) -> str:
        """Get a snapshot of the user's calendar for context"""
        today = date.today()
        start_date = today - timedelta(days=days_back)
        end_date = today + timedelta(days=days_forward)

        result = self.db.client.table("calendar_days").select(
            "date, work_type, cycle_day"
        ).eq(
            "user_id", self.user_id
        ).gte(
            "date", start_date.isoformat()
        ).lte(
            "date", end_date.isoformat()
        ).order("date").execute()

        if not result.data:
            return "No calendar data found. User needs to set up their rotation first."

        # Group by month for readability
        months = {}
        for day in result.data:
            d = datetime.fromisoformat(day["date"])
            month_key = d.strftime("%Y-%m")
            if month_key not in months:
                months[month_key] = []

            work_type = day["work_type"]
            symbol_map = {
                "work_day": "D",
                "work_night": "N",
                "off": "O",
                "blank": "B"
            }
            symbol = symbol_map.get(work_type, "?")
            months[month_key].append(f"{d.day}:{symbol}")

        # Build readable summary
        lines = []
        for month, days in months.items():
            lines.append(f"{month}: {', '.join(days)}")

        return "\n".join(lines)

    async def _get_logs_and_incidents(self, days_back: int = 30) -> str:
        """Get recent logs and incidents for context"""
        today = date.today()
        start_date = today - timedelta(days=days_back)

        # Fetch recent daily logs
        logs_result = self.db.client.table("daily_logs").select(
            "date, note, actual_hours, overtime_hours"
        ).eq(
            "user_id", self.user_id
        ).gte(
            "date", start_date.isoformat()
        ).order("date", desc=True).limit(20).execute()

        # Fetch recent incidents
        incidents_result = self.db.client.table("incidents").select(
            "date, type, severity, title, description"
        ).eq(
            "user_id", self.user_id
        ).gte(
            "date", start_date.isoformat()
        ).order("date", desc=True).limit(20).execute()

        lines = []

        # Format logs
        if logs_result.data:
            lines.append("RECENT DAILY LOGS:")
            for log in logs_result.data:
                log_line = f"  [{log['date']}] {log['note']}"
                if log.get('actual_hours'):
                    log_line += f" (Hours: {log['actual_hours']}h"
                    if log.get('overtime_hours'):
                        log_line += f", OT: {log['overtime_hours']}h"
                    log_line += ")"
                lines.append(log_line)
        else:
            lines.append("RECENT DAILY LOGS: None")

        lines.append("")

        # Format incidents
        if incidents_result.data:
            lines.append("RECENT INCIDENTS:")
            for incident in incidents_result.data:
                severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴", "critical": "⚫"}.get(incident['severity'], "⚪")
                lines.append(f"  [{incident['date']}] {severity_emoji} [{incident['type'].upper()}] {incident['title']}")
                lines.append(f"      {incident['description'][:150]}{'...' if len(incident['description']) > 150 else ''}")
        else:
            lines.append("RECENT INCIDENTS: None")

        return "\n".join(lines) if lines else "No recent logs or incidents."

    async def send_message(
        self,
        content: str,
        auto_execute: bool = False
    ) -> Dict[str, Any]:
        """
        Send a message and get agent response using tool calling.
        """
        # Save user message
        user_message = await self._save_message("user", content)

        # Get full context
        master_settings = await self.settings_service.get_snapshot(self.user_id)
        calendar_snapshot = await self._get_calendar_snapshot()
        logs_and_incidents = await self._get_logs_and_incidents()
        chat_history = await self.get_history(limit=10)

        logger.info(f"[CHAT] User {self.user_id} message: {content[:100]}")
        logger.info(f"[CHAT] Calendar snapshot length: {len(calendar_snapshot)} chars")
        logger.info(f"[CHAT] Logs/incidents length: {len(logs_and_incidents)} chars")

        # Build system prompt with full context
        system_prompt = SYSTEM_PROMPT.format(
            master_settings=json.dumps(master_settings, indent=2, default=str),
            calendar_snapshot=calendar_snapshot,
            logs_and_incidents=logs_and_incidents,
            current_date=datetime.now().strftime("%Y-%m-%d")
        )

        # Build conversation history
        contents = []
        for msg in reversed(chat_history[:-1]):
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

        contents.append(types.Content(role="user", parts=[types.Part(text=content)]))

        # Create tool declarations
        tools = types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters=tool["parameters"]
            )
            for tool in WATCHMAN_TOOLS
        ])

        # Call Gemini with tools
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=[tools],
                    temperature=0.2,
                    max_output_tokens=8000
                )
            )

            # Check for function calls
            function_call = None
            response_text = ""

            if response.candidates and len(response.candidates) > 0:
                candidate = response.candidates[0]
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if hasattr(part, 'function_call') and part.function_call:
                            function_call = part.function_call
                            logger.info(f"[GEMINI] Tool called: {function_call.name}")
                            logger.info(f"[GEMINI] Tool args: {dict(function_call.args)}")
                        elif hasattr(part, 'text') and part.text:
                            response_text = part.text.strip()

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            response_text = f"I'm having trouble processing that right now. Error: {str(e)[:100]}"
            function_call = None

        # Handle function call (tool use)
        command = None
        if function_call:
            command = {
                "action": function_call.name,
                "payload": dict(function_call.args),
                "explanation": response_text or f"Executing {function_call.name}"
            }
            logger.info(f"[GEMINI] Command from tool call: {command}")
        else:
            logger.info(f"[GEMINI] No tool called - conversational response: {response_text[:200]}...")

        result = {
            "user_message": user_message,
            "response": response_text,
            "is_command": command is not None,
            "command": command,
            "proposal": None
        }

        if command:
            if auto_execute:
                # Execute directly
                exec_result = await self.command_executor.execute(
                    command,
                    source="chat",
                    message_id=user_message["id"]
                )
                result["execution"] = exec_result

                if exec_result.get("success"):
                    confirm_text = f"Done! {command.get('explanation', 'Changes applied.')}"
                else:
                    confirm_text = f"There was an issue: {exec_result.get('error', 'Unknown error')}"

                assistant_message = await self._save_message(
                    "assistant",
                    confirm_text,
                    command_id=exec_result.get("command_id")
                )
                result["response"] = confirm_text
            else:
                # Create proposal for approval
                proposal = await self.command_executor.create_proposal(
                    command,
                    message_id=user_message["id"]
                )
                result["proposal"] = proposal

                explanation = command.get("explanation", "I'd like to make some changes.")
                assistant_message = await self._save_message("assistant", explanation)
                result["response"] = explanation
        else:
            assistant_message = await self._save_message("assistant", response_text)

        result["assistant_message"] = assistant_message
        return result

    async def _save_message(
        self,
        role: str,
        content: str,
        command_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Save a message to the database"""
        message_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "role": role,
            "content": content,
            "command_id": command_id,
            "metadata": {}
        }

        result = self.db.client.table("chat_messages").insert(message_data).execute()

        if result.data and len(result.data) > 0:
            return result.data[0]

        raise Exception("Failed to save message")

    async def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get chat history for user"""
        result = self.db.client.table("chat_messages").select("*").eq(
            "user_id", self.user_id
        ).order("created_at", desc=True).limit(limit).execute()

        return result.data if result.data else []

    async def clear_history(self) -> Dict[str, Any]:
        """Clear chat history for user"""
        self.db.client.table("chat_messages").delete().eq(
            "user_id", self.user_id
        ).execute()

        return {"cleared": True}


def create_chat_service(db: Database, user_id: str) -> ChatService:
    """Factory function"""
    return ChatService(db, user_id)

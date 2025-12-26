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
- Set a range of dates to day shifts, night shifts, or off
IMPORTANT: When user says 'set working days to X' or 'change shifts to X', set preserve_off_days=true to keep off days unchanged.
Only set preserve_off_days=false when user explicitly wants ALL days changed (including off days).""",
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
                    "enum": ["work_day", "work_night", "off"],
                    "description": "The work type to set. work_day=day shift, work_night=night shift, off=rest day"
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
- Regenerate calendar from the current pattern""",
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
                    "description": "A known date in YYYY-MM-DD format"
                },
                "anchor_cycle_day": {
                    "type": "integer",
                    "description": "Which day of the cycle the anchor_date falls on (1-based)"
                }
            },
            "required": []
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
        "name": "undo",
        "description": "Undo the last change made to the calendar or settings",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]


# System prompt - now focused on conversation, tools handle execution
SYSTEM_PROMPT = """You are Watchman, an intelligent calendar assistant for shift workers.

=== CURRENT DATE ===
{current_date}

=== USER'S SETTINGS ===
{master_settings}

=== USER'S CALENDAR (Recent/Relevant Days) ===
{calendar_snapshot}

=== YOUR CAPABILITIES ===
You have access to tools that DIRECTLY modify the user's calendar:
- override_days: Change any date range to day shifts, night shifts, or off days
- update_cycle: Set or change the rotation pattern
- add_leave: Block out vacation/leave dates
- add_commitment: Add recurring events like classes
- undo: Revert the last change

IMPORTANT BEHAVIOR:
1. When user asks to change dates → USE the override_days tool immediately
2. When user asks about their calendar → Look at the CALENDAR snapshot above and respond
3. When user wants to set up rotation → USE update_cycle tool
4. NEVER just say "Done" without calling a tool - your words don't change anything!
5. If you see the calendar data above, you CAN tell the user what their current schedule looks like

CONTEXT AWARENESS:
- Look at the calendar snapshot to see what days are currently set to
- Acknowledge what you see: "I see October shows as [X], I'll change that to [Y]"
- You have FULL visibility into the user's calendar data

EXAMPLES:
User: "Set Oct 16 through Dec 14 as day shifts"
→ Call override_days(start_date="2025-10-16", end_date="2025-12-14", work_type="work_day")

User: "What does my November look like?"
→ Look at calendar_snapshot and describe it

User: "I work 5 days, 5 nights, 5 off starting Jan 1"
→ Call update_cycle with pattern and anchor_date
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
            symbol = "D" if work_type == "work_day" else "N" if work_type == "work_night" else "O"
            months[month_key].append(f"{d.day}:{symbol}")

        # Build readable summary
        lines = []
        for month, days in months.items():
            lines.append(f"{month}: {', '.join(days)}")

        return "\n".join(lines)

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
        chat_history = await self.get_history(limit=10)

        logger.info(f"[CHAT] User {self.user_id} message: {content[:100]}")
        logger.info(f"[CHAT] Calendar snapshot length: {len(calendar_snapshot)} chars")

        # Build system prompt with full context
        system_prompt = SYSTEM_PROMPT.format(
            master_settings=json.dumps(master_settings, indent=2, default=str),
            calendar_snapshot=calendar_snapshot,
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

"""
Chat Service
Handles conversation with the user and coordinates Claude tool calls.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, date, timedelta
from uuid import uuid4
from loguru import logger
import copy
import json

import anthropic

from app.database import Database
from app.engines.master_settings_service import MasterSettingsService
from app.engines.calendar_authority import cycle_is_present, projection_for_user
from app.engines.command_executor import CommandExecutor
from app.planning_horizon import horizon_violation, payload_dates
from app.engines.chat_context import (
    INTERNAL_PARSE_MAX_TOKENS,
    annotate_closed_proposals,
    empty_calendar,
    declined_again_reply,
    repeats_declined_proposal,
    anchor_acknowledgement,
    anchor_day_correction,
    chosen_cycle,
    cycle_length,
    is_hidden_chat_message,
    is_internal_chat_content,
    local_today,
    pattern_phrase,
    rotation_status,
    settings_for_prompt,
    sole_anchor_statement,
    strip_model_reasoning,
    unsolicited_blank_override,
    with_unsaved_pattern,
)
from app.engines.claude_chat import (
    USER_FACING_MODEL_ERROR,
    ModelRequestError,
    complete_chat,
    history_to_messages,
)
from app.engines.day_status import (
    CHAT_SAMPLE_DAYS_FORWARD,
    answer_sentence,
    combine_events,
    date_from_query,
    project_day,
    schedule_question_date,
    status_from_stored,
)
from app.engines.missed_shift import calendar_request
from app.engines.proposal_effects import card_command
from app.leave_access import LEAVE_PRO_REPLY, leave_blocked
from app.prompts.registry import insert_row, load_version, version_for


# Shipped v1 text. Request handling uses version_for so the env can select another.
SYSTEM_PROMPT = load_version("chat-v1").text


def _tools_for(prompt) -> list:
    """v1 keeps the shared tool list. A later version may replace update_cycle text."""
    description = prompt.update_cycle_description
    shift = prompt.shift_by_days_description
    if not description and not shift:
        return WATCHMAN_TOOLS
    tools = copy.deepcopy(WATCHMAN_TOOLS)
    for tool in tools:
        if tool["name"] != "update_cycle":
            continue
        if description:
            tool["description"] = description
        if shift:
            tool["parameters"]["properties"]["shift_by_days"]["description"] = shift
    return tools


# Tool declarations. Claude receives these as input_schema tool definitions.
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
Use 'blank' for dates before the user's rotation became stable/consistent.
When the user did not work a day, set work_type to off and preserve_off_days to false. That is a manual override, not a change to the rotation.
A date with no year uses the user's local today. Past tense uses the most recent past date. Name the full date, for example "Mark Thu 15 Oct 2026 as Off".""",
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
- Days before the anchor stay filled from the pattern. Do not mark them blank unless the user explicitly asks""",
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
        "name": "query_calendar",
        "description": (
            "Answer whether one date is a day shift, a night shift, off, or leave. "
            "Use this for every question about a date, including dates outside the "
            "calendar sample, before the anchor, and next year. "
            "The tool result is the whole answer. Do not add a second guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "The date to check, YYYY-MM-DD. Christmas is 12-25 of the upcoming year."
                }
            },
            "required": ["date"]
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



class ChatService:
    """Service for handling chat with Claude using tool calling."""

    def __init__(self, db: Database, user_id: str):
        self.db = db
        self.user_id = user_id
        self.settings_service = MasterSettingsService(db)
        self.command_executor = CommandExecutor(db, user_id)

    async def _get_calendar_snapshot(
        self,
        days_back: int = 30,
        days_forward: int = CHAT_SAMPLE_DAYS_FORWARD,
    ) -> dict:
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
            return empty_calendar()

        # Group by month for readability
        months = {}
        counts = {}
        ordered_dates = []
        for day in result.data:
            raw_date = day.get("date")
            if hasattr(raw_date, "isoformat"):
                raw_date = raw_date.isoformat()
            day_text = str(raw_date)[:10]
            ordered_dates.append(day_text)
            d = datetime.fromisoformat(day_text)
            month_key = d.strftime("%Y-%m")
            if month_key not in months:
                months[month_key] = []

            work_type = day.get("work_type") or "?"
            counts[work_type] = counts.get(work_type, 0) + 1
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
        header = (
            f"Sample around today, not the end of the schedule: {len(result.data)} days "
            f"from {ordered_dates[0]} to {ordered_dates[-1]}. "
            f"day={counts.get('work_day', 0)}, night={counts.get('work_night', 0)}, "
            f"off={counts.get('off', 0)}, blank={counts.get('blank', 0)}."
        )

        return {
            "text": header + "\n" + "\n".join(lines),
            "count": len(result.data),
            "start": ordered_dates[0],
            "end": ordered_dates[-1],
            "counts": counts,
        }

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

    async def _complete_internal(self, content: str) -> Dict[str, Any]:
        """Parse an onboarding schedule without writing chat history.

        The client asks for one JSON object. Tools and the full calendar prompt
        make that call take 15-20s and then leave the prompt in the user's
        conversation. Adaptive thinking stays on, with a 2048 token cap.
        """
        prompt = version_for("onboarding", self.user_id)
        logger.info(
            f"[CHAT] Internal parse for user {self.user_id} "
            f"prompt_version={prompt.id} ({len(content)} chars)"
        )
        try:
            response_text, _tool = await complete_chat(
                system=prompt.text,
                messages=[{"role": "user", "content": content}],
                tools=None,
                max_tokens=INTERNAL_PARSE_MAX_TOKENS,
                thinking={"type": "adaptive"},
            )
        except (ModelRequestError, anthropic.APIError) as exc:
            logger.error(f"[CLAUDE] Internal parse failed for user {self.user_id}: {exc}")
            response_text = USER_FACING_MODEL_ERROR
        response_text = strip_model_reasoning(response_text) or "Could you tell me a bit more?"
        return {
            "user_message": None,
            "assistant_message": None,
            "response": response_text,
            "is_command": False,
            "command": None,
            "proposal": None,
            "internal": True,
            "prompt_version": prompt.id,
        }

    async def send_message(
        self,
        content: str,
        auto_execute: bool = False,
        pattern: Optional[list] = None,
        onboarding_completed: bool = True,
        timezone_name: Optional[str] = None,
        effective_tier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a message and get agent response using tool calling.
        """
        if effective_tier is not None:
            self.command_executor.effective_tier = effective_tier
        if is_internal_chat_content(content):
            return await self._complete_internal(content)
        self.command_executor.effective_tier = effective_tier or ""
        today = local_today(timezone_name)

        # Only an unfinished signup with no saved rotation, and a message that
        # is nothing but an anchor, skips the model. The reply is a proposal.
        picked = chosen_cycle(pattern)
        if onboarding_completed is False:
            anchor = sole_anchor_statement(content, local_today(timezone_name))
            stored = await self._stored_cycle() if anchor is not None else None
            if anchor is not None and stored is None:
                # The reply is a setup instruction the client applies from
                # response.anchor. Leave both turns out of chat_messages so
                # the dashboard history cannot show a bubble with no button.
                cycle = picked
                length = cycle_length(cycle)
                if length and anchor["cycle_day"] > length:
                    reply = anchor_day_correction(anchor, length)
                    anchor = None
                else:
                    reply = anchor_acknowledgement(anchor, cycle)
                result = {
                    "user_message": None,
                    "assistant_message": None,
                    "response": reply,
                    "is_command": False,
                    "command": None,
                    "proposal": None,
                }
                if anchor is not None:
                    result["anchor"] = anchor
                return result

        # Save user message
        user_message = await self._save_message("user", content)

        # Active cycle comes from Master Settings or the cycles table. The
        # calendar grid is loaded in the same turn so a generated 14/14 is
        # visible even when the document cycle is still empty.
        document = await self.settings_service.get_snapshot(self.user_id)
        try:
            projection = await projection_for_user(self.db, self.user_id)
        except Exception as exc:
            logger.warning(f"[CHAT] Could not load active cycle for {self.user_id}: {exc}")
            projection = {}
        calendar = await self._get_calendar_snapshot()
        if not isinstance(calendar, dict):
            calendar = {**empty_calendar(), "text": str(calendar)}
        logs_and_incidents = await self._get_logs_and_incidents()
        recent_proposals = await self._recent_proposals()
        chat_history = annotate_closed_proposals(
            await self.get_history(limit=10),
            recent_proposals,
        )
        master_settings = settings_for_prompt(document, projection)
        master_settings, pattern_pending = with_unsaved_pattern(master_settings, pattern)
        asked = schedule_question_date(content, today)
        if asked is not None:
            answer = await self._answer_about_day(asked, master_settings)
            assistant_message = await self._save_message("assistant", answer)
            return {
                "user_message": user_message,
                "assistant_message": assistant_message,
                "response": answer,
                "is_command": False,
                "command": None,
                "proposal": None,
            }
        applied = rotation_status(
            master_settings.get("cycle"), calendar, pending=pattern_pending
        )

        handled = calendar_request(content, today)
        if handled is not None:
            if handled.clarify:
                assistant_message = await self._save_message("assistant", handled.clarify)
                return {
                    "user_message": user_message,
                    "assistant_message": assistant_message,
                    "response": handled.clarify,
                    "is_command": False,
                    "command": None,
                    "proposal": None,
                }
            response_text = ""
            tool = {"name": "override_days", "input": handled.payload or {}}
            prompt = version_for("chat", self.user_id)
            self._reply_prompt_version = prompt.id
        else:
            response_text = None
            tool = None
            prompt = None

        if handled is None:
            logger.info(f"[CHAT] User {self.user_id} message: {content[:100]}")
            logger.info(f"[CHAT] Calendar snapshot length: {len(calendar.get('text') or '')} chars")
            logger.info(f"[CHAT] Logs/incidents length: {len(logs_and_incidents)} chars")

            # Build system prompt with full context
            prompt = version_for("chat", self.user_id)
            self._reply_prompt_version = prompt.id
            logger.info(f"[CHAT] prompt_version={prompt.id} for user {self.user_id}")
            system_prompt = prompt.text.format(
                master_settings=json.dumps(master_settings, indent=2, default=str),
                calendar_snapshot=calendar.get("text") or "",
                logs_and_incidents=logs_and_incidents,
                rotation_status=applied,
                current_date=today.isoformat(),
            )

            messages = history_to_messages(chat_history, content)

            try:
                response_text, tool = await complete_chat(
                    system=system_prompt,
                    messages=messages,
                    tools=_tools_for(prompt),
                )
            except (ModelRequestError, anthropic.APIError) as e:
                logger.error(f"[CLAUDE] API error for user {self.user_id}: {e}")
                response_text = USER_FACING_MODEL_ERROR
                tool = None

        response_text = strip_model_reasoning(
            response_text,
            cycle_known=cycle_is_present(master_settings.get("cycle")),
        )
        if tool and unsolicited_blank_override(content, tool.get("name") or "", tool.get("input") or {}):
            logger.info(
                f"[CHAT] Dropped unsolicited blank override for {self.user_id}: "
                "days before the anchor stay filled from the pattern"
            )
            tool = None
            response_text = "Days before the anchor stay filled from your pattern."
        if not response_text and not (tool and tool.get("name")):
            phrase = pattern_phrase(picked)
            response_text = (
                f"Your rotation is already chosen: {phrase}."
                if phrase else "Could you tell me a bit more?"
            )

        if tool and tool.get("name") == "query_calendar":
            day = date_from_query(tool.get("input") or {}, today)
            if day is None:
                answer = "I need one date, like 2026-12-25, to say whether you are working."
            else:
                answer = await self._answer_about_day(day, master_settings)
            assistant_message = await self._save_message("assistant", answer)
            return {
                "user_message": user_message,
                "assistant_message": assistant_message,
                "response": answer,
                "is_command": False,
                "command": None,
                "proposal": None,
                "prompt_version": prompt.id,
            }

        command = None
        if tool and tool.get("name") and repeats_declined_proposal(
            content, tool["name"], tool.get("input") or {}, recent_proposals
        ):
            logger.info(
                f"[CHAT] Dropped {tool['name']} for {self.user_id}: "
                "it repeats a declined proposal"
            )
            response_text = declined_again_reply(tool["name"], tool.get("input") or {})
            tool = None
        if tool and tool.get("name") and leave_blocked(
            effective_tier or "", tool["name"], tool.get("input") or {}
        ):
            logger.info(
                f"[CHAT] Leave planning blocked for free user {self.user_id}"
            )
            response_text = LEAVE_PRO_REPLY
            tool = None
        limit_check = getattr(self.command_executor, "commitment_limit_message", None)
        if tool and tool.get("name") == "add_commitment" and limit_check is not None:
            try:
                limit_reply = await limit_check(tool.get("input") or {})
            except Exception as exc:
                logger.warning(
                    f"[CHAT] Commitment count failed for {self.user_id}; "
                    f"leaving the message through: {exc}"
                )
                limit_reply = None
            if limit_reply:
                logger.info(f"[CHAT] Commitment limit blocked for free user {self.user_id}")
                response_text = limit_reply
                tool = None
        horizon_block = None
        if tool and tool.get("name") and (effective_tier or "") == "free":
            horizon_block = horizon_violation(
                "free", payload_dates(tool.get("input") or {})
            )
            if horizon_block:
                logger.info(
                    f"[CHAT] Blocked {tool['name']} past the free horizon for {self.user_id}"
                )
                response_text = horizon_block["message"]
                tool = None
        if tool and tool.get("name"):
            payload = tool.get("input") or {}
            command = card_command({
                "action": tool["name"],
                "payload": payload,
                "explanation": response_text,
            })

        result = {
            "user_message": user_message,
            "response": response_text,
            "is_command": command is not None,
            "command": command,
            "proposal": None,
            "prompt_version": prompt.id,
        }
        if horizon_block:
            result["code"] = horizon_block["code"]
            result["horizon_end"] = horizon_block["horizon_end"]

        if command:
            if auto_execute:
                # Execute directly
                exec_result = await self.command_executor.execute(
                    command,
                    source="chat",
                    message_id=user_message["id"]
                )
                result["execution"] = exec_result

                if exec_result.get("status") == "no_change" or exec_result.get("changed") is False:
                    confirm_text = exec_result.get("message") or "No change."
                elif exec_result.get("success"):
                    confirm_text = f"Done! {command.get('explanation') or 'Changes applied.'}"
                else:
                    logger.error(
                        f"[CHAT] Execution failed for user {self.user_id}: {exec_result.get('error')}"
                    )
                    confirm_text = "There was an issue applying that change. Please try again."

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
                    message_id=user_message["id"],
                    prompt_version=prompt.id,
                )
                result["proposal"] = proposal

                explanation = command.get("explanation", "I'd like to make some changes.")
                assistant_message = await self._save_message("assistant", explanation)
                result["response"] = explanation
        else:
            assistant_message = await self._save_message("assistant", response_text)

        result["assistant_message"] = assistant_message
        return result

    async def _recent_proposals(self) -> list:
        """Recent proposals so a closed change can be marked in the next prompt."""
        client = getattr(self.db, "client", None)
        if client is None:
            return []
        try:
            result = client.table("proposals").select(
                "id, message_id, status, command, expires_at, created_at, reviewed_at"
            ).eq("user_id", self.user_id).order(
                "created_at", desc=True
            ).limit(30).execute()
        except Exception as exc:
            logger.warning(f"[CHAT] Could not load proposals for {self.user_id}: {exc}")
            return []
        return result.data or []

    async def _answer_about_day(self, day, master_settings: dict) -> str:
        """One sentence. A manual override wins over the projected cycle."""
        stored = await self._stored_day(day)
        state = (stored or {}).get("state_json") or {}
        if isinstance(state, dict) and state.get("manual_override"):
            return answer_sentence(status_from_stored(stored))
        table_leaves, table_commitments = await self._table_events()
        leaves, commitments = combine_events(
            master_settings.get("leave_blocks"),
            master_settings.get("commitments"),
            table_leaves,
            table_commitments,
        )
        return answer_sentence(project_day(
            master_settings.get("cycle"),
            day,
            leaves,
            commitments,
        ))

    async def _stored_day(self, day):
        getter = getattr(self.db, "get_calendar_day", None)
        if getter is None:
            return None
        try:
            return await getter(self.user_id, day.isoformat())
        except Exception as exc:
            logger.warning(f"[CHAT] Could not read {day} for {self.user_id}: {exc}")
            return None

    async def _table_events(self):
        leaves = []
        commitments = []
        try:
            get_leave = getattr(self.db, "get_leave_blocks", None)
            get_commitments = getattr(self.db, "get_commitments", None)
            if get_leave:
                leaves = await get_leave(self.user_id) or []
            if get_commitments:
                commitments = await get_commitments(self.user_id) or []
        except Exception as exc:
            logger.warning(f"[CHAT] Could not load leave or commitments for {self.user_id}: {exc}")
        return leaves, commitments

    async def _stored_cycle(self) -> Optional[dict]:
        try:
            document = await self.settings_service.get_snapshot(self.user_id)
        except Exception as exc:
            logger.warning(f"[CHAT] Could not read the cycle for {self.user_id}: {exc}")
            return None
        cycle = document.get("cycle") if isinstance(document, dict) else None
        return cycle if cycle_is_present(cycle) else None

    async def _save_message(
        self,
        role: str,
        content: str,
        command_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Save a message to the database"""
        version = getattr(self, "_reply_prompt_version", None) if role == "assistant" else None
        metadata = {"prompt_version": version} if version else {}
        message_data = {
            "id": str(uuid4()),
            "user_id": self.user_id,
            "role": role,
            "content": content,
            "command_id": command_id,
            "metadata": metadata,
        }
        if version:
            message_data["prompt_version"] = version

        result = insert_row(self.db.client, "chat_messages", message_data)

        if result.data and len(result.data) > 0:
            return result.data[0]

        raise Exception("Failed to save message")

    async def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Visible chat history. Onboarding parse rows are left in the table but not returned."""
        fetch_limit = min(500, max(limit, limit * 4))
        result = self.db.client.table("chat_messages").select("*").eq(
            "user_id", self.user_id
        ).order("created_at", desc=True).limit(fetch_limit).execute()

        rows = result.data if result.data else []
        visible = [row for row in rows if not is_hidden_chat_message(row)]
        return visible[:limit]

    async def clear_history(self) -> Dict[str, Any]:
        """Clear chat history for user"""
        self.db.client.table("chat_messages").delete().eq(
            "user_id", self.user_id
        ).execute()

        return {"cleared": True}


def create_chat_service(db: Database, user_id: str) -> ChatService:
    """Factory function"""
    return ChatService(db, user_id)

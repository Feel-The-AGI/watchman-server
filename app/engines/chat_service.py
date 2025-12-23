"""
Chat Service
Handles conversation with user and coordinates with Gemini for command extraction
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from uuid import uuid4
from loguru import logger
import json
import os

from google import genai
from google.genai import types

from app.database import Database
from app.engines.master_settings_service import MasterSettingsService
from app.engines.command_executor import CommandExecutor


# System prompt template
SYSTEM_PROMPT = """You are Watchman, an intelligent calendar assistant for shift workers.

IMPORTANT: The user's current settings are shown below. DO NOT ask for information that's already here.

=== USER'S CURRENT SETTINGS ===
{master_settings}
=== END SETTINGS ===

If the user has a cycle defined above, USE IT. Don't ask them to repeat it.
If they say "fill my calendar" and a cycle exists, just generate the update_cycle command with the existing pattern.

AVAILABLE COMMANDS:
1. update_cycle - Change work rotation pattern
   Example: {{"action": "update_cycle", "payload": {{"name": "My Rotation", "pattern": [{{"type": "day_shift", "days": 5}}, {{"type": "night_shift", "days": 5}}, {{"type": "off", "days": 5}}], "anchor": {{"date": "2026-01-01", "cycle_day": 4}}, "shift_hours": 12}}, "explanation": "Setting up 5-5-5 rotation"}}

2. add_commitment - Add course, diploma, recurring event
   Example: {{"action": "add_commitment", "payload": {{"name": "Diploma in Survey", "type": "education", "schedule": {{"type": "recurring", "days_of_week": ["tuesday", "thursday"], "time_start": "18:00", "time_end": "20:00", "start_date": "2026-03-15", "end_date": "2027-03-15"}}}}, "explanation": "Adding diploma classes"}}

3. add_leave - Block out leave dates
   Example: {{"action": "add_leave", "payload": {{"name": "Annual Leave", "start_date": "2026-02-10", "end_date": "2026-02-20", "type": "annual"}}, "explanation": "Blocking Feb 10-20 for leave"}}

4. update_constraint - Add/modify scheduling rules
   Example: {{"action": "update_constraint", "payload": {{"rule": "no_study_on_night_shift", "type": "hard", "description": "No study on night shifts"}}, "explanation": "Adding constraint"}}

5. undo - Revert last change
   Example: {{"action": "undo", "payload": {{}}, "explanation": "Undoing last change"}}

RESPONSE FORMAT:
- When user requests a change, output ONLY valid JSON with the command structure above
- When clarification is needed or just chatting, respond conversationally (no JSON)
- For undo/revert requests, use the undo action
- Always include an "explanation" field in commands

RULES:
1. Parse natural language into structured commands
2. Ask clarifying questions when needed
3. Be helpful, concise, and shift-work aware
4. Understand dates, patterns, and schedules naturally
5. For "I work X days, Y nights, Z off" → create update_cycle command
6. For "add my diploma/course/class" → create add_commitment command
7. For "block out/take leave" → create add_leave command
8. For "undo that/go back/revert" → create undo command

Current date: {current_date}
"""


class ChatService:
    """Service for handling chat with Gemini"""
    
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
    
    async def send_message(
        self, 
        content: str,
        auto_execute: bool = False
    ) -> Dict[str, Any]:
        """
        Send a message and get agent response.
        
        Args:
            content: The user's message
            auto_execute: If True, automatically execute commands without proposal
            
        Returns:
            Response with message and optional proposal/command
        """
        # Save user message
        user_message = await self._save_message("user", content)
        
        # Get context
        master_settings = await self.settings_service.get_snapshot(self.user_id)
        chat_history = await self.get_history(limit=10)
        
        # Log what settings we have
        logger.info(f"User {self.user_id} settings: {json.dumps(master_settings, default=str)[:500]}")
        
        # Build messages for Gemini
        system_prompt = SYSTEM_PROMPT.format(
            master_settings=json.dumps(master_settings, indent=2, default=str),
            current_date=datetime.now().strftime("%Y-%m-%d")
        )
        
        # Build conversation history
        contents = []
        for msg in reversed(chat_history[:-1]):  # Exclude the message we just saved
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
        
        # Add current message
        contents.append(types.Content(role="user", parts=[types.Part(text=content)]))
        
        # Call Gemini
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7,
                    max_output_tokens=2048
                )
            )
            
            response_text = response.text.strip()
            
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            logger.error(f"Model: {self.model}, Contents length: {len(contents)}")
            response_text = f"I'm having trouble processing that right now. Error: {str(e)[:100]}"
        
        # Check if response is a command (JSON)
        command = self._extract_command(response_text)
        
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
                
                # Save confirmation message
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
                
                # Save response with proposal context
                explanation = command.get("explanation", "I'd like to make some changes.")
                validation = proposal.get("validation", {})
                
                if validation.get("warnings"):
                    warning_text = "\n\nNote: " + "; ".join(
                        w.get("message", "") for w in validation["warnings"]
                    )
                    explanation += warning_text
                
                if validation.get("violations"):
                    violation_text = "\n\nIssue: " + "; ".join(
                        v.get("message", "") for v in validation["violations"]
                    )
                    explanation += violation_text
                
                assistant_message = await self._save_message("assistant", explanation)
                result["response"] = explanation
        else:
            # Save conversational response
            assistant_message = await self._save_message("assistant", response_text)
        
        result["assistant_message"] = assistant_message
        return result
    
    def _extract_command(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract JSON command from response text"""
        # Try to parse as JSON directly
        try:
            # Remove markdown code blocks if present
            if "```json" in text:
                start = text.find("```json") + 7
                end = text.find("```", start)
                text = text[start:end].strip()
            elif "```" in text:
                start = text.find("```") + 3
                end = text.find("```", start)
                text = text[start:end].strip()
            
            data = json.loads(text)
            if isinstance(data, dict) and "action" in data:
                return data
        except json.JSONDecodeError:
            pass
        
        # Try to find JSON object in text
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                json_str = text[start:end]
                data = json.loads(json_str)
                if isinstance(data, dict) and "action" in data:
                    return data
        except json.JSONDecodeError:
            pass
        
        return None
    
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

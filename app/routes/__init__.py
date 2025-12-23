"""Watchman Routes Module - Conversation-first API"""

from app.routes import auth, cycles, commitments, calendar, stats, settings
from app.routes import chat, commands, master_settings

__all__ = [
    "auth",
    "cycles", 
    "commitments",
    "calendar",
    "stats",
    "settings",
    "chat",
    "commands",
    "master_settings"
]

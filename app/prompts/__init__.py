"""Versioned system prompts. See docs/PROMPTS.md."""

from app.prompts.registry import load_version, version_for

__all__ = ["load_version", "version_for"]

"""
Watchman Services
Business logic and external service integrations
"""

from app.services.email_service import EmailService, get_email_service

__all__ = ["EmailService", "get_email_service"]

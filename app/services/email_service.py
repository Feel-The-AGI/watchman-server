"""
Watchman Email Service
Handles sending email notifications using Resend
"""

import httpx
from typing import Optional
from loguru import logger
from app.config import get_settings


class EmailService:
    """Email service using Resend API"""

    BASE_URL = "https://api.resend.com"

    def __init__(self):
        settings = get_settings()
        self.api_key = settings.resend_api_key
        self.from_email = settings.email_from
        self.enabled = bool(self.api_key)

        if not self.enabled:
            logger.warning("[EMAIL] Resend API key not configured. Emails will be logged but not sent.")

    async def send_email(
        self,
        to: str,
        subject: str,
        html: str,
        text: Optional[str] = None,
    ) -> bool:
        """
        Send an email using Resend API.

        Args:
            to: Recipient email address
            subject: Email subject
            html: HTML content
            text: Plain text content (optional, will be auto-generated if not provided)

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.enabled:
            logger.info(f"[EMAIL] (Not sent - no API key) To: {to}, Subject: {subject}")
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.BASE_URL}/emails",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self.from_email,
                        "to": [to],
                        "subject": subject,
                        "html": html,
                        "text": text,
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"[EMAIL] Sent successfully to {to}: {data.get('id')}")
                    return True
                else:
                    logger.error(f"[EMAIL] Failed to send to {to}: {response.status_code} - {response.text}")
                    return False

        except Exception as e:
            logger.error(f"[EMAIL] Error sending email to {to}: {e}")
            return False

    async def send_schedule_reminder(
        self,
        to: str,
        user_name: str,
        upcoming_work_type: str,
        work_date: str,
        commitments: list,
    ) -> bool:
        """Send a schedule reminder email"""
        commitment_list = ""
        if commitments:
            commitment_list = "<ul>" + "".join(f"<li>{c}</li>" for c in commitments) + "</ul>"
        else:
            commitment_list = "<p>No commitments scheduled.</p>"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
                .header {{ text-align: center; margin-bottom: 24px; }}
                .logo {{ font-size: 24px; font-weight: bold; color: #6366f1; }}
                .content {{ line-height: 1.6; }}
                .highlight {{ background: linear-gradient(135deg, #6366f1, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; font-weight: bold; }}
                .work-type {{ display: inline-block; padding: 8px 16px; border-radius: 8px; font-weight: 600; margin: 8px 0; }}
                .work-type.day {{ background: #f59e0b20; color: #f59e0b; }}
                .work-type.night {{ background: #6366f120; color: #8b5cf6; }}
                .work-type.off {{ background: #10b98120; color: #10b981; }}
                .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
                ul {{ padding-left: 20px; }}
                li {{ margin: 8px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Watchman</div>
                </div>
                <div class="content">
                    <p>Hi {user_name},</p>
                    <p>Here's your schedule reminder for <strong>{work_date}</strong>:</p>
                    <p class="work-type {'day' if 'day' in upcoming_work_type.lower() else 'night' if 'night' in upcoming_work_type.lower() else 'off'}">
                        {upcoming_work_type.replace('_', ' ').title()}
                    </p>
                    <p><strong>Commitments:</strong></p>
                    {commitment_list}
                </div>
                <div class="footer">
                    <p>You're receiving this because you enabled email notifications.</p>
                    <p>Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=to,
            subject=f"Schedule Reminder: {upcoming_work_type.replace('_', ' ').title()} - {work_date}",
            html=html,
        )

    async def send_incident_alert(
        self,
        to: str,
        user_name: str,
        incident_title: str,
        incident_type: str,
        severity: str,
        description: str,
    ) -> bool:
        """Send an incident alert email"""
        severity_colors = {
            "critical": "#dc2626",
            "high": "#ef4444",
            "medium": "#f59e0b",
            "low": "#10b981",
        }
        severity_color = severity_colors.get(severity.lower(), "#6b7280")

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
                .header {{ text-align: center; margin-bottom: 24px; }}
                .logo {{ font-size: 24px; font-weight: bold; color: #6366f1; }}
                .alert-badge {{ display: inline-block; padding: 8px 16px; border-radius: 8px; font-weight: 600; background: {severity_color}20; color: {severity_color}; margin: 8px 0; }}
                .content {{ line-height: 1.6; }}
                .incident-title {{ font-size: 20px; font-weight: bold; margin: 16px 0; }}
                .description {{ background: #0a0a0f; padding: 16px; border-radius: 8px; margin: 16px 0; }}
                .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Watchman</div>
                </div>
                <div class="content">
                    <p>Hi {user_name},</p>
                    <p>A new incident has been logged:</p>
                    <p class="incident-title">{incident_title}</p>
                    <p>
                        <span class="alert-badge">{severity.upper()}</span>
                        <span style="color: #6b7280; margin-left: 8px;">{incident_type.replace('_', ' ').title()}</span>
                    </p>
                    <div class="description">
                        <p style="margin: 0;">{description[:500]}{'...' if len(description) > 500 else ''}</p>
                    </div>
                    <p>
                        <a href="https://trywatchman.app/dashboard/incidents" style="color: #6366f1; font-weight: 600;">View in Watchman →</a>
                    </p>
                </div>
                <div class="footer">
                    <p>You're receiving this because you enabled email notifications.</p>
                    <p>Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=to,
            subject=f"[{severity.upper()}] Incident: {incident_title}",
            html=html,
        )

    async def send_weekly_summary(
        self,
        to: str,
        user_name: str,
        week_start: str,
        week_end: str,
        stats: dict,
    ) -> bool:
        """Send a weekly summary email"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
                .header {{ text-align: center; margin-bottom: 24px; }}
                .logo {{ font-size: 24px; font-weight: bold; color: #6366f1; }}
                .content {{ line-height: 1.6; }}
                .stats-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin: 24px 0; }}
                .stat-card {{ background: #0a0a0f; padding: 16px; border-radius: 12px; text-align: center; }}
                .stat-value {{ font-size: 28px; font-weight: bold; color: #6366f1; }}
                .stat-label {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
                .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Watchman</div>
                </div>
                <div class="content">
                    <p>Hi {user_name},</p>
                    <p>Here's your weekly summary for <strong>{week_start}</strong> to <strong>{week_end}</strong>:</p>
                    <div class="stats-grid">
                        <div class="stat-card">
                            <div class="stat-value">{stats.get('work_days', 0)}</div>
                            <div class="stat-label">Work Days</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">{stats.get('off_days', 0)}</div>
                            <div class="stat-label">Off Days</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">{stats.get('commitments_completed', 0)}</div>
                            <div class="stat-label">Commitments Done</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">{stats.get('incidents', 0)}</div>
                            <div class="stat-label">Incidents Logged</div>
                        </div>
                    </div>
                    <p>
                        <a href="https://trywatchman.app/dashboard" style="color: #6366f1; font-weight: 600;">View your calendar →</a>
                    </p>
                </div>
                <div class="footer">
                    <p>You're receiving this because you enabled email notifications.</p>
                    <p>Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=to,
            subject=f"Your Weekly Summary: {week_start} - {week_end}",
            html=html,
        )

    async def send_welcome_email(
        self,
        to: str,
        user_name: str,
    ) -> bool:
        """Send welcome email from co-founders when user signs up"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
                .header {{ text-align: center; margin-bottom: 24px; }}
                .logo {{ font-size: 28px; font-weight: bold; color: #6366f1; }}
                .content {{ line-height: 1.8; }}
                .highlight {{ color: #6366f1; font-weight: 600; }}
                .cta-button {{ display: inline-block; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: white; padding: 14px 28px; border-radius: 12px; text-decoration: none; font-weight: 600; margin: 24px 0; }}
                .signature {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #ffffff10; }}
                .founders {{ display: flex; gap: 8px; margin-top: 8px; }}
                .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Watchman</div>
                </div>
                <div class="content">
                    <p>Hey {user_name},</p>

                    <p>Welcome to Watchman! We're genuinely excited to have you here.</p>

                    <p>We built Watchman because we know how chaotic shift work can be. Between rotating schedules, tracking overtime, and trying to maintain some work-life balance, it's a lot to manage. We wanted to create something that actually helps.</p>

                    <p>Here's what you can do right now:</p>
                    <ul>
                        <li><strong>Set up your shift pattern</strong> - Tell us your rotation and we'll generate your calendar</li>
                        <li><strong>Track your commitments</strong> - Study goals, side projects, whatever matters to you</li>
                        <li><strong>Log incidents</strong> - Keep a record of workplace issues (trust us, it's important)</li>
                    </ul>

                    <p style="text-align: center;">
                        <a href="https://trywatchman.app/dashboard" class="cta-button">Get Started →</a>
                    </p>

                    <p>If you have any questions or feedback, just reply to this email. We read everything.</p>

                    <div class="signature">
                        <p style="margin-bottom: 4px;">Cheers,</p>
                        <p style="margin: 0;"><strong>Médina & Jason</strong></p>
                        <p style="color: #6b7280; font-size: 14px; margin-top: 4px;">Co-founders, Watchman</p>
                    </div>
                </div>
                <div class="footer">
                    <p>You're receiving this because you signed up for Watchman.</p>
                    <p><a href="https://trywatchman.app" style="color: #6366f1;">trywatchman.app</a></p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=to,
            subject="Welcome to Watchman - Let's get you set up",
            html=html,
        )

    async def send_pro_upgrade_email(
        self,
        to: str,
        user_name: str,
    ) -> bool:
        """Send thank you email when user upgrades to Pro"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e5e5e5; padding: 20px; }}
                .container {{ max-width: 600px; margin: 0 auto; background: #1a1a2e; border-radius: 16px; padding: 32px; }}
                .header {{ text-align: center; margin-bottom: 24px; }}
                .logo {{ font-size: 28px; font-weight: bold; color: #6366f1; }}
                .pro-badge {{ display: inline-block; background: linear-gradient(135deg, #f59e0b, #d97706); color: white; padding: 6px 16px; border-radius: 20px; font-size: 14px; font-weight: 600; margin: 8px 0; }}
                .content {{ line-height: 1.8; }}
                .feature-list {{ background: #0a0a0f; border-radius: 12px; padding: 20px; margin: 24px 0; }}
                .feature-item {{ display: flex; align-items: center; gap: 12px; margin: 12px 0; }}
                .check {{ color: #10b981; font-size: 18px; }}
                .cta-button {{ display: inline-block; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: white; padding: 14px 28px; border-radius: 12px; text-decoration: none; font-weight: 600; margin: 24px 0; }}
                .signature {{ margin-top: 32px; padding-top: 24px; border-top: 1px solid #ffffff10; }}
                .footer {{ margin-top: 32px; text-align: center; color: #6b7280; font-size: 12px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Watchman</div>
                    <span class="pro-badge">PRO</span>
                </div>
                <div class="content">
                    <p>Hey {user_name},</p>

                    <p><strong>Thank you so much for upgrading to Pro!</strong></p>

                    <p>Your support means the world to us. Seriously. We're a small team, and every Pro member helps us keep building and improving Watchman.</p>

                    <div class="feature-list">
                        <p style="margin: 0 0 16px 0; font-weight: 600;">You now have access to:</p>
                        <div class="feature-item">
                            <span class="check">✓</span>
                            <span><strong>Calendar Sharing</strong> - Share your schedule with anyone</span>
                        </div>
                        <div class="feature-item">
                            <span class="check">✓</span>
                            <span><strong>Weighted Constraints</strong> - Advanced scheduling with priorities</span>
                        </div>
                        <div class="feature-item">
                            <span class="check">✓</span>
                            <span><strong>PDF Exports</strong> - Beautiful reports for incidents & logs</span>
                        </div>
                        <div class="feature-item">
                            <span class="check">✓</span>
                            <span><strong>Priority Support</strong> - We've got your back</span>
                        </div>
                    </div>

                    <p style="text-align: center;">
                        <a href="https://trywatchman.app/dashboard" class="cta-button">Explore Pro Features →</a>
                    </p>

                    <p>Got ideas for new features? We're all ears. Just reply to this email.</p>

                    <div class="signature">
                        <p style="margin-bottom: 4px;">With gratitude,</p>
                        <p style="margin: 0;"><strong>Médina & Jason</strong></p>
                        <p style="color: #6b7280; font-size: 14px; margin-top: 4px;">Co-founders, Watchman</p>
                    </div>
                </div>
                <div class="footer">
                    <p>Manage your subscription at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app/settings</a></p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=to,
            subject="You're now a Watchman Pro - Thank you!",
            html=html,
        )


# Singleton instance
_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    """Get email service singleton"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service

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
            commitment_list = "<ul style='padding-left: 20px; color: #e5e5e5;'>" + "".join(f"<li style='color: #e5e5e5; margin: 8px 0;'>{c}</li>" for c in commitments) + "</ul>"
        else:
            commitment_list = "<p style='color: #9ca3af;'>No commitments scheduled.</p>"

        # Determine work type styling
        work_type_lower = upcoming_work_type.lower()
        if 'day' in work_type_lower:
            work_bg = "rgba(245, 158, 11, 0.2)"
            work_color = "#f59e0b"
        elif 'night' in work_type_lower:
            work_bg = "rgba(99, 102, 241, 0.2)"
            work_color = "#8b5cf6"
        else:
            work_bg = "rgba(16, 185, 129, 0.2)"
            work_color = "#10b981"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 24px; font-weight: bold; color: #6366f1;">Watchman</div>
                </div>
                <div style="line-height: 1.6; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Hi {user_name},</p>
                    <p style="color: #e5e5e5;">Here's your schedule reminder for <strong style="color: #ffffff;">{work_date}</strong>:</p>
                    <p style="display: inline-block; padding: 8px 16px; border-radius: 8px; font-weight: 600; margin: 8px 0; background: {work_bg}; color: {work_color};">
                        {upcoming_work_type.replace('_', ' ').title()}
                    </p>
                    <p style="color: #e5e5e5;"><strong style="color: #ffffff;">Commitments:</strong></p>
                    {commitment_list}
                </div>
                <div style="margin-top: 32px; text-align: center; color: #9ca3af; font-size: 12px;">
                    <p style="color: #9ca3af;">You're receiving this because you enabled email notifications.</p>
                    <p style="color: #9ca3af;">Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
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
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 24px; font-weight: bold; color: #6366f1;">Watchman</div>
                </div>
                <div style="line-height: 1.6; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Hi {user_name},</p>
                    <p style="color: #e5e5e5;">A new incident has been logged:</p>
                    <p style="font-size: 20px; font-weight: bold; margin: 16px 0; color: #ffffff;">{incident_title}</p>
                    <p style="color: #e5e5e5;">
                        <span style="display: inline-block; padding: 8px 16px; border-radius: 8px; font-weight: 600; background: {severity_color}20; color: {severity_color}; margin: 8px 0;">{severity.upper()}</span>
                        <span style="color: #9ca3af; margin-left: 8px;">{incident_type.replace('_', ' ').title()}</span>
                    </p>
                    <div style="background-color: #0a0a0f; padding: 16px; border-radius: 8px; margin: 16px 0;">
                        <p style="margin: 0; color: #e5e5e5;">{description[:500]}{'...' if len(description) > 500 else ''}</p>
                    </div>
                    <p style="color: #e5e5e5;">
                        <a href="https://trywatchman.app/dashboard/incidents" style="color: #6366f1; font-weight: 600;">View in Watchman →</a>
                    </p>
                </div>
                <div style="margin-top: 32px; text-align: center; color: #9ca3af; font-size: 12px;">
                    <p style="color: #9ca3af;">You're receiving this because you enabled email notifications.</p>
                    <p style="color: #9ca3af;">Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
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
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 24px; font-weight: bold; color: #6366f1;">Watchman</div>
                </div>
                <div style="line-height: 1.6; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Hi {user_name},</p>
                    <p style="color: #e5e5e5;">Here's your weekly summary for <strong style="color: #ffffff;">{week_start}</strong> to <strong style="color: #ffffff;">{week_end}</strong>:</p>

                    <table style="width: 100%; border-collapse: separate; border-spacing: 8px; margin: 24px 0;">
                        <tr>
                            <td style="background-color: #0a0a0f; padding: 16px; border-radius: 12px; text-align: center; width: 50%;">
                                <div style="font-size: 28px; font-weight: bold; color: #6366f1;">{stats.get('work_days', 0)}</div>
                                <div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">Work Days</div>
                            </td>
                            <td style="background-color: #0a0a0f; padding: 16px; border-radius: 12px; text-align: center; width: 50%;">
                                <div style="font-size: 28px; font-weight: bold; color: #10b981;">{stats.get('off_days', 0)}</div>
                                <div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">Off Days</div>
                            </td>
                        </tr>
                        <tr>
                            <td style="background-color: #0a0a0f; padding: 16px; border-radius: 12px; text-align: center; width: 50%;">
                                <div style="font-size: 28px; font-weight: bold; color: #f59e0b;">{stats.get('commitments_completed', 0)}</div>
                                <div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">Commitments</div>
                            </td>
                            <td style="background-color: #0a0a0f; padding: 16px; border-radius: 12px; text-align: center; width: 50%;">
                                <div style="font-size: 28px; font-weight: bold; color: #ef4444;">{stats.get('incidents', 0)}</div>
                                <div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">Incidents</div>
                            </td>
                        </tr>
                    </table>

                    <p style="color: #e5e5e5;">
                        <a href="https://trywatchman.app/dashboard" style="color: #6366f1; font-weight: 600;">View your calendar →</a>
                    </p>
                </div>
                <div style="margin-top: 32px; text-align: center; color: #9ca3af; font-size: 12px;">
                    <p style="color: #9ca3af;">You're receiving this because you enabled email notifications.</p>
                    <p style="color: #9ca3af;">Manage your preferences at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app</a></p>
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
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 28px; font-weight: bold; color: #6366f1;">Watchman</div>
                </div>
                <div style="line-height: 1.8; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Hey {user_name},</p>

                    <p style="color: #e5e5e5;">Welcome to Watchman! We're genuinely excited to have you here.</p>

                    <p style="color: #e5e5e5;">We built Watchman because we know how chaotic shift work can be. Between rotating schedules, tracking overtime, and trying to maintain some work-life balance, it's a lot to manage. We wanted to create something that actually helps.</p>

                    <p style="color: #e5e5e5;">Here's what you can do right now:</p>
                    <ul style="color: #e5e5e5;">
                        <li style="color: #e5e5e5; margin: 8px 0;"><strong style="color: #ffffff;">Set up your shift pattern</strong> - Tell us your rotation and we'll generate your calendar</li>
                        <li style="color: #e5e5e5; margin: 8px 0;"><strong style="color: #ffffff;">Track your commitments</strong> - Study goals, side projects, whatever matters to you</li>
                        <li style="color: #e5e5e5; margin: 8px 0;"><strong style="color: #ffffff;">Log incidents</strong> - Keep a record of workplace issues (trust us, it's important)</li>
                    </ul>

                    <p style="text-align: center;">
                        <a href="https://trywatchman.app/dashboard" style="display: inline-block; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #ffffff; padding: 14px 28px; border-radius: 12px; text-decoration: none; font-weight: 600; margin: 24px 0;">Get Started →</a>
                    </p>

                    <p style="color: #e5e5e5;">If you have any questions or feedback, just reply to this email. We read everything.</p>

                    <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid rgba(255,255,255,0.1);">
                        <p style="margin-bottom: 4px; color: #e5e5e5;">Cheers,</p>
                        <p style="margin: 0; color: #ffffff;"><strong>Médina & Jason</strong></p>
                        <p style="color: #9ca3af; font-size: 14px; margin-top: 4px;">Co-founders, Watchman</p>
                    </div>
                </div>
                <div style="margin-top: 32px; text-align: center; color: #9ca3af; font-size: 12px;">
                    <p style="color: #9ca3af;">You're receiving this because you signed up for Watchman.</p>
                    <p style="color: #9ca3af;"><a href="https://trywatchman.app" style="color: #6366f1;">trywatchman.app</a></p>
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
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 28px; font-weight: bold; color: #6366f1;">Watchman</div>
                    <span style="display: inline-block; background: linear-gradient(135deg, #f59e0b, #d97706); color: #ffffff; padding: 6px 16px; border-radius: 20px; font-size: 14px; font-weight: 600; margin: 8px 0;">PRO</span>
                </div>
                <div style="line-height: 1.8; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Hey {user_name},</p>

                    <p style="color: #e5e5e5;"><strong style="color: #ffffff;">Thank you so much for upgrading to Pro!</strong></p>

                    <p style="color: #e5e5e5;">Your support means the world to us. Seriously. We're a small team, and every Pro member helps us keep building and improving Watchman.</p>

                    <div style="background-color: #0a0a0f; border-radius: 12px; padding: 20px; margin: 24px 0;">
                        <p style="margin: 0 0 16px 0; font-weight: 600; color: #ffffff;">You now have access to:</p>
                        <div style="margin: 12px 0; color: #e5e5e5;">
                            <span style="color: #10b981; font-size: 18px; margin-right: 12px;">✓</span>
                            <span style="color: #e5e5e5;"><strong style="color: #ffffff;">Calendar Sharing</strong> - Share your schedule with anyone</span>
                        </div>
                        <div style="margin: 12px 0; color: #e5e5e5;">
                            <span style="color: #10b981; font-size: 18px; margin-right: 12px;">✓</span>
                            <span style="color: #e5e5e5;"><strong style="color: #ffffff;">Weighted Constraints</strong> - Advanced scheduling with priorities</span>
                        </div>
                        <div style="margin: 12px 0; color: #e5e5e5;">
                            <span style="color: #10b981; font-size: 18px; margin-right: 12px;">✓</span>
                            <span style="color: #e5e5e5;"><strong style="color: #ffffff;">PDF Exports</strong> - Beautiful reports for incidents & logs</span>
                        </div>
                        <div style="margin: 12px 0; color: #e5e5e5;">
                            <span style="color: #10b981; font-size: 18px; margin-right: 12px;">✓</span>
                            <span style="color: #e5e5e5;"><strong style="color: #ffffff;">Priority Support</strong> - We've got your back</span>
                        </div>
                    </div>

                    <p style="text-align: center;">
                        <a href="https://trywatchman.app/dashboard" style="display: inline-block; background: linear-gradient(135deg, #6366f1, #8b5cf6); color: #ffffff; padding: 14px 28px; border-radius: 12px; text-decoration: none; font-weight: 600; margin: 24px 0;">Explore Pro Features →</a>
                    </p>

                    <p style="color: #e5e5e5;">Got ideas for new features? We're all ears. Just reply to this email.</p>

                    <div style="margin-top: 32px; padding-top: 24px; border-top: 1px solid rgba(255,255,255,0.1);">
                        <p style="margin-bottom: 4px; color: #e5e5e5;">With gratitude,</p>
                        <p style="margin: 0; color: #ffffff;"><strong>Médina & Jason</strong></p>
                        <p style="color: #9ca3af; font-size: 14px; margin-top: 4px;">Co-founders, Watchman</p>
                    </div>
                </div>
                <div style="margin-top: 32px; text-align: center; color: #9ca3af; font-size: 12px;">
                    <p style="color: #9ca3af;">Manage your subscription at <a href="https://trywatchman.app/dashboard/settings" style="color: #6366f1;">trywatchman.app/settings</a></p>
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

    async def send_admin_new_subscriber_notification(
        self,
        admin_email: str,
        subscriber_email: str,
        subscriber_name: str,
        amount_usd: float,
    ) -> bool:
        """Send notification to admin when someone subscribes"""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
        </head>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0a0a0f; color: #e5e5e5; padding: 20px; margin: 0;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #1a1a2e; border-radius: 16px; padding: 32px;">
                <div style="text-align: center; margin-bottom: 24px;">
                    <div style="font-size: 28px; font-weight: bold; color: #10b981;">New Pro Subscriber!</div>
                </div>
                <div style="line-height: 1.8; color: #e5e5e5;">
                    <p style="color: #e5e5e5;">Someone just upgraded to Watchman Pro!</p>
                    
                    <div style="background-color: #0a0a0f; border-radius: 12px; padding: 20px; margin: 24px 0;">
                        <p style="margin: 8px 0; color: #e5e5e5;"><strong style="color: #ffffff;">Name:</strong> {subscriber_name}</p>
                        <p style="margin: 8px 0; color: #e5e5e5;"><strong style="color: #ffffff;">Email:</strong> {subscriber_email}</p>
                        <p style="margin: 8px 0; color: #e5e5e5;"><strong style="color: #ffffff;">Amount:</strong> ${int(amount_usd)}/month</p>
                    </div>
                    
                    <p style="color: #10b981; font-size: 24px; text-align: center;">💰</p>
                </div>
            </div>
        </body>
        </html>
        """

        return await self.send_email(
            to=admin_email,
            subject=f"💰 New Pro Subscriber: {subscriber_name}",
            html=html,
        )


# Singleton instance
_email_service: Optional[EmailService] = None

# Admin email for notifications
ADMIN_EMAIL = "notifications@trywatchman.app"  # Add admin email address


def get_email_service() -> EmailService:
    """Get email service singleton"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service

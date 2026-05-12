"""Email notification for L2 escalations.

Reads SMTP configuration from environment variables:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM

If any required variable is missing, escalation proceeds but email is skipped
and a warning is logged — never raises.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.mime.text import MIMEText

from src.l1_agent.utils.logging import get_logger

logger = get_logger("email_notifier")


def send_escalation_email(team_email: str, subject: str, body: str) -> bool:
    """Send an escalation email to *team_email*.

    Returns True if the email was sent successfully, False otherwise.
    Never raises — all failures are logged as warnings.
    """
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port_str = os.getenv("SMTP_PORT", "587")
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not smtp_host:
        logger.warning(
            "SMTP_HOST not configured — skipping escalation email to %s", team_email
        )
        return False

    try:
        smtp_port = int(smtp_port_str)
    except ValueError:
        logger.warning("Invalid SMTP_PORT '%s', defaulting to 587", smtp_port_str)
        smtp_port = 587

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = team_email

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls(context=context)
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.sendmail(smtp_from, [team_email], msg.as_string())
        logger.info("Escalation email sent to %s (subject: %s)", team_email, subject)
        return True
    except Exception as exc:
        logger.warning("Failed to send escalation email to %s: %s", team_email, exc)
        return False

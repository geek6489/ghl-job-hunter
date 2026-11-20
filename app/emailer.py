"""Email delivery through Resend's API or any SMTP server."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import get_config
from .http import post_json

log = logging.getLogger(__name__)


class EmailNotConfigured(Exception):
    pass


def send_email(to: list[str], subject: str, html: str, text: str) -> None:
    cfg = get_config()
    if not cfg.email_enabled:
        raise EmailNotConfigured("Email provider is not configured (see EMAIL_* variables)")
    if not to:
        raise EmailNotConfigured("No recipient: set EMAIL_TO or the address in Settings")
    if cfg.email_provider == "smtp":
        msg = EmailMessage()
        msg["From"] = cfg.email_from
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        if cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context(), timeout=30) as s:
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as s:
                if cfg.smtp_use_tls:
                    s.starttls(context=ssl.create_default_context())
                if cfg.smtp_username:
                    s.login(cfg.smtp_username, cfg.smtp_password)
                s.send_message(msg)
        return
    post_json(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {cfg.resend_api_key}", "Content-Type": "application/json"},
        json={"from": cfg.email_from, "to": to, "subject": subject, "html": html, "text": text},
    )

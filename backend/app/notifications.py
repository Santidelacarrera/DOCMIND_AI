"""Outbound email for workspace invitations.

Selection mirrors ``app.storage``: a provider is picked once from configuration
and the module-level singleton is used everywhere. ``ConsoleEmailProvider`` is the
default so local development and tests never need real SMTP credentials; it is
rejected in staging/production by ``Settings.validate_runtime_configuration``.
"""

import logging
import smtplib
from email.message import EmailMessage
from typing import ClassVar, Protocol

from app.core import settings

logger = logging.getLogger("docmind.email")


class EmailProvider(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailProvider:
    """Logs the message instead of sending it. Development/test only."""

    sent: ClassVar[list[tuple[str, str, str]]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        ConsoleEmailProvider.sent.append((to, subject, body))
        logger.info("email.console to=%s subject=%s", to, subject)


class SMTPEmailProvider:
    def send(self, to: str, subject: str, body: str) -> None:
        config = settings()
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.smtp_from
        message["To"] = to
        message.set_content(body)
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10) as server:
            if config.smtp_use_tls:
                server.starttls()
            if config.smtp_username:
                server.login(config.smtp_username, (config.smtp_password or "").get_secret_value())
            server.send_message(message)


def build_email_provider() -> EmailProvider:
    return SMTPEmailProvider() if settings().smtp_host else ConsoleEmailProvider()


email_provider: EmailProvider = build_email_provider()


def invitation_email(organization_name: str, inviter_email: str, role: str, accept_url: str) -> tuple[str, str]:
    subject = f"You're invited to join {organization_name} on DocMind AI"
    body = (
        f"{inviter_email} invited you to join \"{organization_name}\" on DocMind AI as {role.lower()}.\n\n"
        f"Accept the invitation: {accept_url}\n\n"
        f"This link expires in {settings().invitation_expiry_hours} hours. "
        "If you weren't expecting this, you can ignore this email."
    )
    return subject, body

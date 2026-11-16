"""Send the password reset link. Mail stays off until SMTP settings are filled in."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from spe.config import settings


def mail_configured() -> bool:
    return bool(settings.smtp_host.strip() and settings.smtp_from.strip())


def send_password_reset(email: str, token: str) -> None:
    send_link(email, token, route="reset", subject="Reset your Caliber password",
              purpose="Use this link to choose a new password.")


def send_email_verification(email: str, token: str) -> None:
    send_link(email, token, route="verify", subject="Verify your Caliber email",
              purpose="Use this link to verify your email address.")


def send_link(email: str, token: str, *, route: str, subject: str, purpose: str) -> None:
    origin = next((part.strip().rstrip("/") for part in str(settings.web_origin or "").split(",") if part.strip()), "")
    if not origin.startswith("https://") and settings.app_env == "prod":
        raise ValueError("Production email requires an HTTPS web origin")
    link = f"{origin}/{route}?token={token}"
    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = email
    message["Subject"] = subject
    message.set_content(
        f"{purpose} It expires in one hour.\n\n"
        f"{link}\n\n"
        "If you did not ask for this, you can ignore the email.\n"
    )
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)

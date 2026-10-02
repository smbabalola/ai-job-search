"""SMTP email adapter (one concrete DP-2 option): STARTTLS, optional login,
multipart text + HTML, the outbox idempotency key as a header."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Mapping

from webapp.comms.email_port import PermanentSendError, TransientSendError

IDEMPOTENCY_HEADER = "X-JobSearch-Idempotency-Key"


class SmtpEmailProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, username: str | None, password: str | None, starttls: bool,
                 from_address: str, *, timeout: float = 30.0) -> None:
        self.host, self.port = host, int(port)
        self.username, self.password = username, password
        self.starttls = starttls
        self.from_address = from_address
        self.timeout = timeout

    def send(self, *, to: str, subject: str, text: str, html: str, idempotency_key: str,
             headers: Mapping[str, str]) -> str:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = to
        message["Subject"] = subject
        message_id = make_msgid(domain=self.from_address.rsplit("@", 1)[-1].strip(">") or None)
        message["Message-ID"] = message_id
        message[IDEMPOTENCY_HEADER] = idempotency_key
        for name, value in headers.items():
            message[name] = value
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
                client.ehlo()
                if self.starttls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError) as exc:
            raise PermanentSendError(f"smtp refused: {exc}") from None
        except smtplib.SMTPResponseException as exc:
            if 500 <= exc.smtp_code < 600:
                raise PermanentSendError(f"smtp {exc.smtp_code}") from None
            raise TransientSendError(f"smtp {exc.smtp_code}") from None
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientSendError(f"smtp unavailable: {type(exc).__name__}") from None
        return message_id

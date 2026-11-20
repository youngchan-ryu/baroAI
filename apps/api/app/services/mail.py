"""Bounded SMTP transport. Outcomes never imply recipient delivery.

Only DATA's final positive reply means accepted. A disconnect while DATA is in
flight is unknown; replaying it would risk duplicates. No SMTP response text is
returned or logged. Transport functions run on a worker thread, outside DB tx.
"""

import json
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from app.smtp_policy import smtp_policy_error


@dataclass(frozen=True)
class Outcome:
    state: str
    error_code: str | None = None


def local_save(settings, job_id, payload):
    path = Path(settings.mail_sink_path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            return Outcome("failed", "local_path_refused")
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            return Outcome("failed", "local_permissions")
        target = path / f"{job_id}.json"
        data = {**payload, "delivery": "local", "external_sent": False, "job_id": str(job_id)}
        # Deterministic name permits recovery after successful fsync + lost DB ACK.
        if target.exists():
            if target.is_symlink() or target.stat().st_mode & 0o077:
                return Outcome("failed", "local_permissions")
            descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as handle:
                if os.fstat(handle.fileno()).st_uid != os.getuid():
                    return Outcome("failed", "local_permissions")
                return (
                    Outcome("local_saved")
                    if json.load(handle) == data
                    else Outcome("unknown", "local_conflict")
                )
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return Outcome("local_saved")
    except (OSError, ValueError):
        return Outcome("failed", "local_write_failed")


def smtp_send(settings, job_id, payload):
    # Defense in depth: internal callers cannot bypass Settings validation.
    policy_error = smtp_policy_error(settings)
    if policy_error:
        return Outcome("failed", policy_error)
    if (
        not isinstance(payload, dict)
        or any(not isinstance(payload.get(k), str) for k in ("to", "subject", "body"))
        or any(c in payload["to"] + payload["subject"] for c in "\r\n")
        or len(payload["body"].encode()) > 16384
    ):
        return Outcome("failed", "payload_invalid")
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    message["Message-ID"] = f"<{job_id}@baroai.local>"
    message.set_content(payload["body"])
    client = None
    data_started = False
    try:
        context = ssl.create_default_context()
        if settings.smtp_security == "tls":
            client = smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
            )
        code, _ = client.ehlo()
        if not 200 <= code < 300:
            return rejection(code)
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
            code, _ = client.ehlo()
            if not 200 <= code < 300:
                return rejection(code)
        if settings.smtp_user.get_secret_value():
            client.login(
                settings.smtp_user.get_secret_value(), settings.smtp_pass.get_secret_value()
            )
        code, _ = client.mail(settings.mail_from)
        if not 200 <= code < 300:
            return rejection(code)
        code, _ = client.rcpt(payload["to"])
        if not 200 <= code < 300:
            return rejection(code)
        # smtplib.data combines the 354 and final response. Once entered we
        # conservatively treat any connection loss as possibly accepted.
        data_started = True
        code, _ = client.data(message.as_bytes())
        if 200 <= code < 300:
            return Outcome("accepted")
        return rejection(code)
    except smtplib.SMTPDataError as exc:
        return rejection(exc.smtp_code)
    except ssl.SSLError:
        return (
            Outcome("unknown", "smtp_result_unknown")
            if data_started
            else Outcome("failed", "smtp_tls_failed")
        )
    except smtplib.SMTPAuthenticationError:
        return Outcome("failed", "smtp_auth_failed")
    except (OSError, smtplib.SMTPException):
        return (
            Outcome("unknown", "smtp_result_unknown")
            if data_started
            else Outcome("retry_wait", "smtp_pre_data_unavailable")
        )
    finally:
        if client:
            # close avoids QUIT blocking after DATA; it cannot change acceptance.
            try:
                client.close()
            except (OSError, smtplib.SMTPException):
                pass


def rejection(code):
    return (
        Outcome("retry_wait", "smtp_temporarily_rejected")
        if 400 <= code < 500
        else Outcome("failed", "smtp_rejected")
    )

"""Fail-closed SMTP transport settings; no network access or secret reporting."""

import re

_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL = re.compile(rf"{_ATOM}(?:\.{_ATOM})*\Z")


def smtp_policy_error(settings):
    """Return a safe error code or None; callers must not log settings."""
    host = settings.smtp_host
    loopback = host in {"127.0.0.1", "::1"}
    if not loopback:
        if getattr(settings, "smtp_external_enabled", False) is not True:
            return "smtp_host_blocked"
        if not isinstance(host, str) or not _HOST.fullmatch(host):
            return "smtp_host_blocked"
    if settings.smtp_security not in {"tls", "starttls", "plain_test"}:
        return "smtp_security_invalid"
    if settings.smtp_security == "plain_test" and (not loopback or settings.app_env != "test"):
        return "smtp_cleartext_blocked"
    if not (isinstance(settings.smtp_port, int) and 1 <= settings.smtp_port <= 65535):
        return "smtp_configuration_invalid"
    if not (
        isinstance(settings.smtp_timeout_seconds, int) and 1 <= settings.smtp_timeout_seconds <= 15
    ):
        return "smtp_configuration_invalid"
    sender = settings.mail_from
    if not isinstance(sender, str) or len(sender) > 254 or sender.count("@") != 1:
        return "smtp_sender_invalid"
    local, domain = sender.split("@")
    if (
        len(local) > 64
        or not _LOCAL.fullmatch(local)
        or not (_HOST.fullmatch(domain) or (loopback and domain == "localhost"))
    ):
        return "smtp_sender_invalid"
    user = settings.smtp_user.get_secret_value()
    password = settings.smtp_pass.get_secret_value()
    if bool(user) != bool(password) or (not loopback and not user):
        return "smtp_auth_configuration_invalid"
    return None

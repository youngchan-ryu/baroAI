"""Gmail send-only transport and private OAuth storage; never logs credentials."""

import base64
import fcntl
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path

from app.services.mail import Outcome

SCOPE = "https://www.googleapis.com/auth/gmail.send"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(url, data, headers):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
        return response.status, json.loads(response.read(65537))


def gmail_policy_error(settings):
    if (
        settings.mail_delivery != "gmail"
        or settings.gmail_external_enabled is not True
        or settings.deployment_mode != "service"
        or settings.app_env not in {"dev", "test"}
    ):
        return "gmail_disabled"
    sender = settings.mail_from
    if (
        not isinstance(sender, str)
        or not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sender
        )
        or len(sender) > 254
    ):
        return "gmail_sender_invalid"
    return None


def private_dir(path, *, test_mode=False):
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("private_path_invalid")
    root = Path.home() / "Library/Application Support/BaroAI"
    test_root = Path(tempfile.gettempdir()).resolve()
    if path.is_relative_to(root):
        check_root = Path.home()
    elif test_mode and path.is_relative_to(test_root):
        check_root = test_root
    else:
        raise ValueError("private_path_invalid")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("private_path_invalid")
    current = check_root
    for part in path.relative_to(check_root).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("private_permissions")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private_permissions")
    return path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 65536
        ):
            raise ValueError("private_permissions")
        return json.load(handle)


def write_private(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".oauth-")
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked_store(path, *, test_mode=False):
    path = private_dir(path, test_mode=test_mode)
    fd = os.open(path / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("private_permissions")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        os.close(fd)


def token_response(data, *, refresh_required):
    if not isinstance(data, dict) or set(str(data.get("scope", "")).split()) != {SCOPE}:
        raise ValueError("scope_invalid")
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ValueError("token_invalid")
    if refresh_required and (
        not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]
    ):
        raise ValueError("refresh_missing")
    return data


def access_token(directory, *, test_mode=False):
    with locked_store(directory, test_mode=test_mode) as path:
        token = read_private(path / "token.json")
        if token.get("token_uri") != TOKEN_URI or token.get("scope") != SCOPE:
            raise ValueError("token_invalid")
        form = urllib.parse.urlencode(
            {
                "client_id": token["client_id"],
                "client_secret": token["client_secret"],
                "refresh_token": token["refresh_token"],
                "grant_type": "refresh_token",
            }
        ).encode()
        _, response = post(TOKEN_URI, form, {"Content-Type": "application/x-www-form-urlencoded"})
        # Google may omit scope on refresh; the persisted original grant remains authoritative.
        response.setdefault("scope", token["scope"])
        token_response(response, refresh_required=False)
        if response.get("refresh_token"):
            token["refresh_token"] = response["refresh_token"]
            write_private(path / "token.json", token)
        return response["access_token"]


def gmail_send(settings, job_id, payload, *, deadline=None, cancelled=None):
    deadline = deadline if deadline is not None else time.monotonic() + 120
    error = gmail_policy_error(settings)
    if error:
        return Outcome("failed", error)
    if (
        not isinstance(payload, dict)
        or any(not isinstance(payload.get(k), str) for k in ("to", "subject", "body"))
        or any(c in payload["to"] + payload["subject"] for c in "\r\n")
        or len(payload["body"].encode()) > 16384
    ):
        return Outcome("failed", "payload_invalid")
    try:
        access = access_token(settings.gmail_credentials_dir, test_mode=settings.app_env == "test")
    except Exception:
        return Outcome("failed", "gmail_token_unavailable")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = (
        settings.mail_from,
        payload["to"],
        payload["subject"],
    )
    message["Message-ID"] = f"<{job_id}@baroai.local>"
    message.set_content(payload["body"])
    data = json.dumps({"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}).encode()
    if time.monotonic() >= deadline or (cancelled and cancelled.is_set()):
        return Outcome("failed", "gmail_send_deadline")
    try:
        status, response = post(
            SEND_URI,
            data,
            {"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
        )
        if 200 <= status < 300 and isinstance(response, dict) and response.get("id"):
            return Outcome("accepted")
        return Outcome("unknown", "gmail_result_unknown")
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in {408, 429}:
            return Outcome("failed", "gmail_rejected")
        return Outcome("unknown", "gmail_result_unknown")
    except Exception:
        return Outcome("unknown", "gmail_result_unknown")

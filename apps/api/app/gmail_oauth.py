"""Owner-run Desktop OAuth; the service never invokes this command."""

import argparse
import base64
import hashlib
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.services.gmail import (
    SCOPE,
    TOKEN_URI,
    locked_store,
    post,
    read_private,
    token_response,
    write_private,
)

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_DIR = Path.home() / "Library/Application Support/BaroAI/gmail"


def optional_token(path):
    try:
        return read_private(path / "token.json")
    except FileNotFoundError:
        return None


def connect(directory, login_hint):
    with locked_store(directory) as path:
        client = read_private(path / "client.json").get("installed", {})
        previous = optional_token(path)
    if (
        client.get("token_uri") != TOKEN_URI
        or not client.get("client_id")
        or not client.get("client_secret")
    ):
        raise ValueError("desktop_client_invalid")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    result = {}
    deadline = time.monotonic() + 300

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            remaining = max(0.01, deadline - time.monotonic())
            self.connection.settimeout(min(5, remaining))

            def stop():
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            self.timer = threading.Timer(remaining, stop)
            self.timer.daemon = True
            self.timer.start()

        def finish(self):
            try:
                super().finish()
            finally:
                self.timer.cancel()

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = (
                time.monotonic() < deadline
                and parsed.path == "/callback"
                and query.get("state") == [state]
                and len(query.get("code", [])) == 1
                and "error" not in query
            )
            if valid:
                result["code"] = query["code"][0]
            self.send_response(200 if valid else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Authorization received." if valid else b"Invalid callback.")

        def log_message(self, format, *args):
            pass

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        params = {
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": login_hint,
        }
        print("브라우저에서 발신 계정을 직접 확인하고 gmail.send 권한만 승인하세요.")
        if not webbrowser.open(AUTH_URI + "?" + urllib.parse.urlencode(params)):
            raise ValueError("browser_unavailable")
        while not result and time.monotonic() < deadline:
            server.timeout = max(0.01, deadline - time.monotonic())
            server.handle_request()
    if not result or time.monotonic() >= deadline:
        raise ValueError("authorization_timeout")
    form = urllib.parse.urlencode(
        {
            "code": result["code"],
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
        }
    ).encode()
    _, response = post(TOKEN_URI, form, {"Content-Type": "application/x-www-form-urlencoded"})
    token_response(response, refresh_required=True)
    with locked_store(directory) as path:
        if (
            optional_token(path) != previous
            or read_private(path / "client.json").get("installed") != client
        ):
            raise ValueError("credentials_changed")
        write_private(
            path / "token.json",
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "token_uri": TOKEN_URI,
                "scope": SCOPE,
                "refresh_token": response["refresh_token"],
            },
        )


def main():
    parser = argparse.ArgumentParser(description="Connect the owner's Gmail send-only account")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--login-hint", required=True)
    args = parser.parse_args()
    try:
        connect(args.directory, args.login_hint)
    except Exception:
        raise SystemExit(
            "gmail_oauth_failed (check private client file, permissions and consent)"
        ) from None
    print("연결 정보 저장 완료. 메일 발송 및 수신은 아직 확인되지 않았습니다.")


if __name__ == "__main__":
    main()

"""Owned loopback-only Redis lifecycle. Never prints credentials or kills a PID."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PORT = 56379


class RedisError(RuntimeError):
    pass


class Client:
    def __init__(self, password: str | None = None):
        self.sock = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        self.file = self.sock.makefile("rb")
        if password is not None:
            try:
                self.command("AUTH", password)
            except Exception:
                self.close()
                raise

    def close(self):
        self.file.close()
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self):
        line = self.file.readline()
        if not line:
            raise EOFError("Redis connection closed")
        kind, value = line[:1], line[1:-2]
        if kind == b"-":
            raise RedisError("Redis rejected command")
        if kind == b"+":
            return value.decode()
        if kind == b":":
            return int(value)
        if kind == b"$":
            length = int(value)
            if length == -1:
                return None
            data = self.file.read(length)
            if self.file.read(2) != b"\r\n":
                raise RedisError("Invalid Redis response")
            return data.decode()
        if kind == b"*":
            return [self.read() for _ in range(int(value))]
        raise RedisError("Invalid Redis response")

    def command(self, *args):
        encoded = [str(arg).encode() for arg in args]
        request = f"*{len(encoded)}\r\n".encode()
        for value in encoded:
            request += f"${len(value)}\r\n".encode() + value + b"\r\n"
        self.sock.sendall(request)
        return self.read()


class LocalRedis:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.base = self.root / ".local/redis"
        self.data = self.base / "data"
        self.marker = self.base / "owner.json"
        self.password_file = self.base / "password"
        self.config = self.base / "redis.conf"

    def owned(self):
        for path in (self.root / ".local", self.base, self.data, self.marker, self.password_file, self.config):
            if path.is_symlink():
                raise RedisError("Refusing symbolic link in owned Redis paths")
        try:
            marker = json.loads(self.marker.read_text())
        except (OSError, ValueError):
            raise RedisError("Redis ownership marker missing or invalid") from None
        if marker != {"root": str(self.root), "port": PORT}:
            raise RedisError("Redis ownership marker mismatch")
        if any(p.stat().st_uid != os.getuid() for p in
               (self.base, self.data, self.marker, self.password_file, self.config)):
            raise RedisError("Redis filesystem owner mismatch")
        if self.base.stat().st_mode & 0o077 or self.data.stat().st_mode & 0o077:
            raise RedisError("Redis directories must be private")
        if any(p.stat().st_mode & 0o077 for p in
               (self.marker, self.password_file, self.config)):
            raise RedisError("Redis settings must be private")

    def password(self):
        self.owned()
        return self.password_file.read_text().strip()

    def initialize(self):
        if self.base.exists():
            self.owned()
            return
        if (self.root / ".local").is_symlink():
            raise RedisError("Refusing symbolic link in Redis parent")
        self.base.mkdir(parents=True, mode=0o700)
        self.data.mkdir(mode=0o700)
        password = secrets.token_urlsafe(48)
        files = {
            self.marker: json.dumps({"root": str(self.root), "port": PORT}),
            self.password_file: password + "\n",
            self.config: self.render_config(password),
        }
        for path, content in files.items():
            with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
                file.write(content)

    def render_config(self, password):
        return (
                f'bind 127.0.0.1\nport {PORT}\nprotected-mode yes\n'
                f'requirepass {password}\ndir "{self.data}"\n'
                'appendonly yes\nappendfsync everysec\nsave ""\n'
                f'daemonize yes\npidfile "{self.base / "redis.pid"}"\n'
                f'logfile "{self.base / "redis.log"}"\n'
            )

    def validate_config(self):
        password = self.password()
        if not password or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in password):
            raise RedisError("Invalid private Redis credential format")
        if self.config.read_bytes() != self.render_config(password).encode():
            raise RedisError("Redis configuration differs from approved project configuration")

    def verify(self, client):
        self.owned()
        values = dict(zip(*(iter(client.command("CONFIG", "GET", "dir", "port", "bind",
                                              "protected-mode", "appendonly", "appendfsync")),) * 2))
        expected = {"port": str(PORT), "bind": "127.0.0.1", "protected-mode": "yes",
                    "appendonly": "yes", "appendfsync": "everysec"}
        if Path(values.get("dir", "")).resolve() != self.data.resolve() or any(
            values.get(key) != value for key, value in expected.items()
        ):
            raise RedisError("Redis server ownership/configuration mismatch")

    def status(self):
        with Client(self.password()) as client:
            self.verify(client)
            if client.command("PING") != "PONG":
                raise RedisError("Redis health check failed")

    def start(self):
        self.owned()
        self.validate_config()
        try:
            self.status()
            return
        except ConnectionRefusedError:
            pass
        binary = os.environ.get("BARO_REDIS_SERVER") or shutil.which("redis-server")
        if not binary:
            raise RedisError("redis-server missing; install Redis first")
        # Do not launch over an unknown listener, including one refusing authentication.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", PORT))
        result = subprocess.run([binary, str(self.config)], capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise RedisError("Redis start failed; inspect private local log")
        for _ in range(50):
            try:
                self.status()
                return
            except ConnectionRefusedError:
                time.sleep(0.1)
        raise RedisError("Redis start timed out")

    def stop(self):
        # AUTH, ownership check and shutdown share one connection (no PID kill fallback).
        with Client(self.password()) as client:
            self.verify(client)
            try:
                client.command("SHUTDOWN")
            except EOFError:
                pass
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    pass
            except ConnectionRefusedError:
                return
            time.sleep(0.1)
        raise RedisError("Redis shutdown timed out")

    def configure(self):
        password = self.password()
        path = self.root / ".env"
        if not path.is_file() or path.is_symlink():
            raise RedisError("Create the project .env first; refusing missing/symlink file")
        values = {"REDIS_URL": f"redis://:{quote(password, safe='')}@127.0.0.1:{PORT}/0",
                  "TEST_REDIS_URL": f"redis://:{quote(password, safe='')}@127.0.0.1:{PORT}/1"}
        with path.open(newline="") as file:
            lines = file.read().splitlines(keepends=True)
        seen = set()
        for index, line in enumerate(lines):
            key, separator, value = line.strip().partition("=")
            if key not in values or not separator:
                continue
            if key in seen:
                raise RedisError("Duplicate Redis environment setting")
            seen.add(key)
            if value not in ("", "redis://localhost:6379/0", values[key]):
                raise RedisError("Existing Redis URL preserved; refusing replacement")
            lines[index] = f"{key}={values[key]}\n"
        for key in values.keys() - seen:
            lines.append(f"\n{key}={values[key]}\n")
        # Preserve every unrelated byte, including API keys and LLM_FAKE.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", dir=path.parent, prefix=".env.redis-", delete=False
            ) as file:
                temporary = Path(file.name)
                os.fchmod(file.fileno(), 0o600)
                file.write("".join(lines))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "start", "status", "stop", "configure"))
    args = parser.parse_args()
    local = LocalRedis()
    try:
        if args.action == "init":
            local.initialize()
        else:
            getattr(local, args.action)()
    except (OSError, ValueError, RedisError, EOFError, subprocess.SubprocessError):
        print("Redis action failed safely; check installation, private files and port ownership.",
              file=sys.stderr)
        return 1
    print(f"Redis {args.action}: OK (127.0.0.1:{PORT}; credentials hidden)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

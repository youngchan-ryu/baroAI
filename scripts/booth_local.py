"""Prepare and supervise private, synthetic local booth generations."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import resource
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlsplit


class BoothError(Exception):
    pass


shutdown_requested = False
promotion_ready = False
promotion_requested = False


def request_promotion(_signum, _frame):
    global promotion_requested
    if promotion_ready:
        promotion_requested = True


def request_shutdown(_signum, _frame):
    global shutdown_requested
    shutdown_requested = True


def ensure_continue():
    if shutdown_requested:
        raise BoothError("shutdown_requested")


class OwnedChildren:
    """Manage only foreground children created by this live supervisor process."""

    def __init__(self, *, shutdown_timeout=10):
        self.children = []
        self.expected_stop_codes = {}
        self.shutdown_timeout = shutdown_timeout

    def start(
        self,
        command,
        *,
        env,
        cwd,
        expected_stop_codes=(0, -signal.SIGTERM),
        input_text=None,
    ):
        ensure_continue()
        if (
            signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
            or threading.active_count() != 1
        ):
            raise BoothError("unsafe_child_reaping")
        child = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            preexec_fn=disable_core,  # noqa: PLW1509 -- single-threaded supervisor is required
        )
        self.children.append(child)
        self.expected_stop_codes[child] = frozenset(expected_stop_codes)
        if input_text is not None:
            try:
                child.stdin.write(input_text.encode())
                child.stdin.close()
            except (BrokenPipeError, OSError):
                raise BoothError("private_child_input_failed") from None
        ensure_continue()
        return child

    def close(self):
        failed = []
        unconfirmed = False
        for child in reversed(self.children):
            try:
                if child.poll() is not None:
                    unconfirmed = True
                else:
                    child.terminate()
                    try:
                        returncode = child.wait(timeout=self.shutdown_timeout)
                        # Foreground services may exit cleanly or be terminated by our SIGTERM.
                        if returncode not in self.expected_stop_codes.get(
                            child, (0, -signal.SIGTERM)
                        ):
                            unconfirmed = True
                    except subprocess.TimeoutExpired:
                        unconfirmed = True
                        child.kill()
                        child.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                failed.append(child)
        self.children = list(reversed(failed))
        self.expected_stop_codes = {
            child: self.expected_stop_codes[child]
            for child in failed
            if child in self.expected_stop_codes
        }
        if failed or unconfirmed:
            raise BoothError("child_shutdown_unconfirmed")

    def stop(self, child, *, allow_exited=False):
        if child not in self.children:
            raise BoothError("child_shutdown_unconfirmed")
        code = child.poll()
        if code is None:
            child.terminate()
            try:
                code = child.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                raise BoothError("child_shutdown_unconfirmed") from None
            if code not in self.expected_stop_codes[child]:
                raise BoothError("child_shutdown_unconfirmed")
        elif not allow_exited:
            raise BoothError("child_shutdown_unconfirmed")
        self.children.remove(child)
        del self.expected_stop_codes[child]


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
SHOWCASE_REBASE_SOCKET = "rebase.sock"
SHOWCASE_CAMPUS_PROFILE = "r11"


def postgres_bin() -> Path:
    override = os.environ.get("BARO_PG_BIN")
    if override:
        directory = Path(override)
        if not directory.is_absolute():
            raise BoothError("absolute_path_required")
        candidates = (directory,)
    elif sys.platform.startswith("linux"):
        candidates = (Path("/usr/lib/postgresql/16/bin"),)
    else:
        candidates = (
            Path("/opt/homebrew/opt/postgresql@16/bin"),
            Path("/usr/local/opt/postgresql@16/bin"),
        )
    for directory in candidates:
        if all(
            executable_file(directory / name)
            for name in ("initdb", "postgres", "psql", "pg_isready")
        ):
            return directory
    raise BoothError("required_tool_missing")


def executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def service_binary(name: str) -> Path:
    # Resolve in the supervisor. Children only receive their explicit, private environment.
    found = shutil.which(name)
    if found:
        return Path(found).absolute()
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            candidate = Path(prefix) / name
            if executable_file(candidate):
                return candidate
    raise BoothError("required_tool_missing")


def system_lsof() -> Path:
    # Listener ownership depends on this system tool's output.
    for location in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        candidate = Path(location)
        if executable_file(candidate):
            return candidate
    raise BoothError("required_tool_missing")


def disable_core():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def open_parent(path: Path):
    if not path.is_absolute() or any(
        part in {".", ".."} for part in str(path).split("/")
    ):
        raise BoothError("absolute_path_required")
    parts = path.parts
    if len(parts) < 2:
        raise BoothError("invalid_path")
    fd = os.open("/", DIR_FLAGS)
    try:
        for part in parts[1:-1]:
            child = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise BoothError("unsafe_parent")
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def open_private_dir(parent, name):
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise BoothError("unsafe_directory")
    return fd


def write_private(parent, name, content):
    fd = os.open(name, FILE_FLAGS, 0o600, dir_fd=parent)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(content, file, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())


def read_private(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise BoothError("unsafe_marker")
        with os.fdopen(fd, encoding="utf-8", closefd=False) as file:
            return json.load(file)
    finally:
        os.close(fd)


def initialize(path: Path):
    parent, name = open_parent(path)
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        root = open_private_dir(parent, name)
        try:
            info = os.fstat(root)
            write_private(
                root,
                "owner.json",
                {
                    "version": 1,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "uid": os.getuid(),
                },
            )
        finally:
            os.close(root)
    finally:
        os.close(parent)


def prepare_generation(path: Path, *, memory_only=False):
    parent, name = open_parent(path)
    try:
        root = open_private_dir(parent, name)
    finally:
        os.close(parent)
    try:
        info = os.fstat(root)
        marker = read_private(root, "owner.json")
        if marker != {
            "version": 1,
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": os.getuid(),
        }:
            raise BoothError("owner_mismatch")
        generation = uuid.uuid4().hex
        os.mkdir(generation, 0o700, dir_fd=root)
        gen = open_private_dir(root, generation)
        try:
            os.mkdir("storage", 0o700, dir_fd=gen)
            value = {
                "version": 1,
                "generation": generation,
                "database": f"booth_{generation}",
                "role": f"booth_{generation}",
                "db_password": secrets.token_urlsafe(32),
                "admin_password": secrets.token_urlsafe(32),
                "redis_password": secrets.token_urlsafe(32),
                "jwt_secret": secrets.token_urlsafe(48),
                "export_key": base64.urlsafe_b64encode(
                    secrets.token_bytes(32)
                ).decode(),
                "state": "prepared_only",
            }
            marker = {
                key: value[key]
                for key in ("version", "generation", "database", "role", "state")
            }
            write_private(gen, "generation.json", marker if memory_only else value)
            os.fsync(gen)
        finally:
            os.close(gen)
        os.fsync(root)
    finally:
        os.close(root)
    return (generation, value) if memory_only else generation


def generation_environment(
    path: Path,
    generation: str,
    value: dict,
    *,
    db_port: int,
    redis_port: int,
    web_port: int,
):
    """Build a closed environment for a generated local generation, never inherit secrets."""
    if (
        len(generation) != 32
        or any(c not in "0123456789abcdef" for c in generation)
        or value.get("generation") != generation
        or value.get("database") != f"booth_{generation}"
        or value.get("role") != f"booth_{generation}"
        or value.get("state") != "prepared_only"
    ):
        raise BoothError("invalid_generation")
    for key in (
        "db_password",
        "admin_password",
        "redis_password",
        "jwt_secret",
        "export_key",
    ):
        secret = value.get(key)
        if not isinstance(secret, str) or len(secret) < 32:
            raise BoothError("missing_generation_secret")
        if not re.fullmatch(r"[A-Za-z0-9_=-]+", secret):
            raise BoothError("invalid_generation_secret")
    ports = (db_port, redis_port, web_port)
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports):
        raise BoothError("invalid_port")
    if len(set(ports)) != len(ports):
        raise BoothError("port_conflict")
    db_password = quote(value["db_password"], safe="")
    redis_password = quote(value["redis_password"], safe="")
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(path / generation),
        "LC_ALL": "C",
        "BARO_ISOLATED_SETTINGS": "1",
        "APP_ENV": "dev",
        "DEPLOYMENT_MODE": "demo",
        "DATABASE_URL": (
            f"postgresql+asyncpg://{value['role']}:{db_password}"
            f"@127.0.0.1:{db_port}/{value['database']}"
        ),
        "REDIS_URL": f"redis://:{redis_password}@127.0.0.1:{redis_port}/0",
        "STORAGE_PATH": str(path / generation / "storage"),
        "APP_BASE_URL": f"http://127.0.0.1:{web_port}",
        "JWT_SECRET": value["jwt_secret"],
        "EXPORT_ENCRYPTION_KEY": value["export_key"],
        "LLM_FAKE": "true",
        "MAIL_DELIVERY": "disabled",
        "SMTP_EXTERNAL_ENABLED": "false",
        "DEMO_AUTH": "true",
        "AUTO_CONFIRM": "false",
        "SERVICE_NAMESPACE": "baro_service",
        "MAIL_SINK_PATH": str(path / generation / "mail"),
        "MAIL_ENCRYPTION_KEY": "",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": "587",
        "SMTP_USER": "",
        "SMTP_PASS": "",
        "MAIL_FROM": "baroai@localhost",
        "SMTP_SECURITY": "starttls",
        "SMTP_TIMEOUT_SECONDS": "5",
        "OTP_PEPPER": "",
        "ALLOWED_EMAIL_DOMAIN": "snu.ac.kr",
        "SECURE_COOKIE": "false",
        "SESSION_HOURS": "12",
        "WORKER_TOKEN_HOURS": "24",
        "LLM_PRIMARY": "openai",
    }


def claim_generation(path: Path, generation: str, *, memory_value=None):
    """Claim exactly one freshly prepared generation. Claims are never removed."""
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise BoothError("invalid_generation")
    parent, name = open_parent(path)
    try:
        root = open_private_dir(parent, name)
    finally:
        os.close(parent)
    try:
        info = os.fstat(root)
        if read_private(root, "owner.json") != {
            "version": 1,
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": os.getuid(),
        }:
            raise BoothError("owner_mismatch")
        gen = open_private_dir(root, generation)
        try:
            if set(os.listdir(gen)) != {"generation.json", "storage"}:
                raise BoothError("generation_not_fresh")
            storage = open_private_dir(gen, "storage")
            try:
                if os.listdir(storage):
                    raise BoothError("storage_not_empty")
            finally:
                os.close(storage)
            marker = read_private(gen, "generation.json")
            if memory_value is not None:
                expected = {
                    key: memory_value[key]
                    for key in ("version", "generation", "database", "role", "state")
                }
                if marker != expected:
                    raise BoothError("invalid_generation")
                value = memory_value
            else:
                value = marker
            # Validate configuration before recording the irreversible claim.
            generation_environment(
                path, generation, value, db_port=61001, redis_port=61002, web_port=61003
            )
            # macOS has a 104-byte sun_path, including the terminating NUL.
            if len(os.fsencode(path / generation / "socket" / ".s.PGSQL.65535")) >= 104:
                raise BoothError("socket_path_too_long")
            write_private(gen, "claim.json", {"version": 1, "state": "claimed"})
            os.fsync(gen)
            return gen, value
        except BaseException:
            os.close(gen)
            raise
    finally:
        os.close(root)


def verify_generation_path(path: Path, gen_fd: int):
    info = os.stat(path, follow_symlinks=False)
    expected = os.fstat(gen_fd)
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
        expected.st_dev,
        expected.st_ino,
    ):
        raise BoothError("generation_path_changed")


def open_showcase_rebase(home: Path, gen_fd: int) -> socket.socket:
    """Open the owner-only control socket of the running showcase."""
    verify_generation_path(home, gen_fd)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(home / SHOWCASE_REBASE_SOCKET))
    except BaseException:
        listener.close()
        raise
    try:
        os.chmod(SHOWCASE_REBASE_SOCKET, 0o600, dir_fd=gen_fd)
        listener.listen(1)
        listener.setblocking(False)
    except BaseException:
        close_showcase_rebase(listener, gen_fd)
        raise
    return listener


def close_showcase_rebase(listener: socket.socket, gen_fd: int) -> None:
    listener.close()
    try:
        os.unlink(SHOWCASE_REBASE_SOCKET, dir_fd=gen_fd)
    except FileNotFoundError:
        pass


def serve_showcase_rebase(listener: socket.socket, *, home: Path, gen_fd: int,
                          env: dict, repo: Path, generation: str, api_port: int,
                          switch_mode=None) -> dict:
    try:
        request, _ = listener.accept()
    except BlockingIOError:
        return env
    with request:
        request.settimeout(1)
        command = b""
        try:
            with request.makefile("rb") as reader:
                command = reader.readline(1024)
            verify_generation_path(home, gen_fd)
            if command == b"rebase\n" and env["LLM_FAKE"] == "true":
                output = run_private(
                    [str(repo / ".venv/bin/python"), str(repo / "scripts/seed_booth_scenarios.py"),
                     "--api-port", str(api_port), "--generation", generation,
                     "--allow-replay", "--rebase-showcase"],
                    env=env, cwd=repo, timeout=30, seed_diagnostics=True,
                )
                if not re.fullmatch(r"SHOWCASE_REBASED [1-9][0-9]*", output):
                    raise BoothError("unexpected_rebase_result")
            elif switch_mode is not None and command.startswith(b"mode ") and command.endswith(b"\n"):
                payload = json.loads(command[5:])
                if payload == {"mode": "replay"}:
                    env, output = switch_mode(env)
                elif (isinstance(payload, dict) and set(payload) == {"mode", "origin", "budget"}
                      and payload["mode"] == "live"):
                    env, output = switch_mode(
                        env, public_origin=validate_public_origin(payload["origin"]),
                        daily_budget_usd=validate_booth_budget(payload["budget"]),
                    )
                else:
                    raise BoothError("invalid_arguments")
            else:
                raise BoothError("invalid_arguments")
        except (BoothError, OSError, subprocess.TimeoutExpired, ValueError):
            output = "REBASE_REFUSED" if command == b"rebase\n" else "BOOTH_MODE_REFUSED"
        try:
            request.sendall(output.encode() + b"\n")
        except OSError:
            pass
    return env


def request_showcase_rebase(path: Path, generation: str) -> str:
    output = request_showcase_control(path, generation, b"rebase\n", timeout=40)
    if not re.fullmatch(r"SHOWCASE_REBASED [1-9][0-9]*", output):
        raise BoothError("rebase_refused")
    return output


def request_showcase_mode(path: Path, generation: str, *, public_origin=None,
                          daily_budget_usd="2.00") -> str:
    payload = {"mode": "replay"}
    if public_origin is not None:
        payload = {
            "mode": "live", "origin": validate_public_origin(public_origin),
            "budget": validate_booth_budget(daily_budget_usd),
        }
    command = b"mode " + json.dumps(payload).encode("ascii") + b"\n"
    output = request_showcase_control(path, generation, command, timeout=175)
    if not re.fullmatch(r"BOOTH_MODE (?:replay|live photo=(?:available|manual))", output):
        raise BoothError("booth_mode_refused")
    return output


def request_showcase_control(path: Path, generation: str, command: bytes, *, timeout: int) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise BoothError("invalid_generation")
    home = path / generation
    parent, name = open_parent(home)
    try:
        gen_fd = open_private_dir(parent, name)
    finally:
        os.close(parent)
    try:
        verify_generation_path(home, gen_fd)
        socket_info = os.stat(SHOWCASE_REBASE_SOCKET, dir_fd=gen_fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.getuid()
                or socket_info.st_mode & 0o077):
            raise BoothError("unsafe_rebase_socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(home / SHOWCASE_REBASE_SOCKET))
            client.sendall(command)
            with client.makefile("rb") as response:
                output = response.readline(80).decode().strip()
        return output
    finally:
        os.close(gen_fd)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_private_text(parent, name, content):
    fd = os.open(name, FILE_FLAGS, 0o600, dir_fd=parent)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def run_private(command, *, env, cwd, input_text=None, timeout=30, seed_diagnostics=False):
    ensure_continue()
    if (
        threading.active_count() != 1
        or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
    ):
        raise BoothError("unsafe_child_reaping")
    process = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
        preexec_fn=disable_core,  # noqa: PLW1509 -- single-threaded supervisor is required
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + timeout
        pending = input_text
        while True:
            ensure_continue()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                output, error_output = process.communicate(
                    input=pending, timeout=min(0.2, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                pending = None
        ensure_continue()
    except BaseException:
        # The unreaped foreground leader anchors this process group identity.
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    if process.returncode:
        if seed_diagnostics:
            print("SEED_FAILURE", safe_seed_failure(error_output), file=sys.stderr, flush=True)
        raise BoothError("private_command_failed")
    return output.strip()


def safe_seed_failure(stderr: str) -> str:
    """Expose only fixed failure categories, never subprocess text or inputs."""
    reasons = {
        "atomic_location_category_unconfirmed", "atomic_worker_field_unavailable", "atomic_worker_schedule_stale", "atomic_worker_day_off",
        "ticket_revision_conflict", "catalog_revision_conflict", "assignment_snapshot_conflict",
        "location_relationship_conflict",
        "runtime_mismatch", "catalog_mismatch", "worker_profiles_missing",
        "ticket_identity", "ticket_analysis_incomplete", "ticket_draft_missing",
        "assignment_identity", "assignment_missing", "credential_outcome_unknown",
        "report_analysis_incomplete", "live_report_revision_unknown", "live_report_identity",
        "report_draft_missing", "unexpected_state", "live_ledger_invalid",
        "live_report_ledger_invalid", "owned_demo_required", "database_identity",
        "deployment_identity", "worker_identity", "state_identity",
        "showcase_ai_input_changed", "showcase_followup_unmatched", "future_showcase_event",
        "http_400", "http_401", "http_403", "http_404", "http_409", "http_422",
        "http_429", "http_500", "http_502", "http_503", "http_504",
        "HTTPError", "OSError", "ValueError", "KeyError", "StopIteration",
    }
    reasons |= {f"http_{stage}_{code}" for stage in
                ("assign", "start", "report", "confirm", "review", "ticket")
                for code in (400, 401, 403, 404, 409, 422, 429, 500, 502, 503, 504)}
    lines = stderr.strip().splitlines()
    if lines and lines[-1].startswith("STOP "):
        reason = lines[-1][5:]
        if reason in reasons:
            return reason
    if lines:
        exception_type = lines[-1].partition(":")[0]
        if exception_type in {"TypeError", "AttributeError", "AssertionError"}:
            return exception_type
    return "unclassified"


def pg_query(
    psql: Path, *, host: str, port: int, admin: str, password: str, home: Path, sql: str
):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C",
        "PGPASSWORD": password,
        "PGCONNECT_TIMEOUT": "2",
    }
    return run_private(
        [
            str(psql),
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            admin,
            "-d",
            "postgres",
        ],
        env=env,
        cwd=home,
        input_text=sql + "\n",
        timeout=5,
    )


def check_postgres(
    child,
    data: Path,
    socket_dir: Path,
    port: int,
    admin: str,
    password: str,
    psql: Path,
    home: Path,
):
    if child.poll() is not None:
        raise BoothError("postgres_exited")
    pidfile = data / "postmaster.pid"
    lines = pidfile.read_text().splitlines()
    if (
        len(lines) < 4
        or lines[0] != str(child.pid)
        or lines[1] != str(data)
        or lines[3] != str(port)
    ):
        raise BoothError("postgres_identity_mismatch")
    query = "SELECT current_setting('data_directory') || '|' || system_identifier FROM pg_control_system();"
    local = pg_query(
        psql,
        host=str(socket_dir),
        port=port,
        admin=admin,
        password=password,
        home=home,
        sql=query,
    )
    if not local.startswith(str(data) + "|"):
        raise BoothError("postgres_identity_mismatch")
    tcp = pg_query(
        psql,
        host="127.0.0.1",
        port=port,
        admin=admin,
        password=password,
        home=home,
        sql=query,
    )
    if tcp != local or child.poll() is not None:
        raise BoothError("postgres_identity_mismatch")
    return local


def start_postgres(
    children,
    *,
    gen_fd,
    generation_path: Path,
    port: int,
    value: dict,
    pg_bin: Path,
    memory_only=False,
):
    data = generation_path / "postgres"
    socket_dir = generation_path / "socket"
    admin = "booth_admin_" + value["generation"]
    base_env = {"PATH": "/usr/bin:/bin", "HOME": str(generation_path), "LC_ALL": "C"}
    os.mkdir("socket", 0o700, dir_fd=gen_fd)
    if not memory_only:
        write_private_text(gen_fd, "postgres-password", value["admin_password"] + "\n")
    verify_generation_path(generation_path, gen_fd)
    run_private(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(data),
            "-U",
            admin,
            "--pwfile",
            "/dev/stdin" if memory_only else str(generation_path / "postgres-password"),
            "--auth-local=scram-sha-256",
            "--auth-host=scram-sha-256",
            "--encoding=UTF8",
            "--locale=C",
        ],
        env=base_env,
        cwd=generation_path,
        timeout=60,
        input_text=value["admin_password"] + "\n" if memory_only else None,
    )
    verify_generation_path(generation_path, gen_fd)
    child = children.start(
        [
            str(pg_bin / "postgres"),
            "-D",
            str(data),
            "-c",
            "listen_addresses=127.0.0.1",
            "-c",
            f"port={port}",
            "-c",
            f"unix_socket_directories={socket_dir}",
            "-c",
            "unix_socket_permissions=0700",
            "-c",
            "logging_collector=off",
        ],
        env=base_env,
        cwd=generation_path,
    )
    verify_generation_path(generation_path, gen_fd)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ensure_continue()
        if child.poll() is not None:
            raise BoothError("postgres_exited")
        try:
            run_private(
                [
                    str(pg_bin / "pg_isready"),
                    "-q",
                    "-h",
                    str(socket_dir),
                    "-p",
                    str(port),
                    "-U",
                    admin,
                ],
                env=base_env,
                cwd=generation_path,
                timeout=3,
            )
            break
        except BoothError:
            time.sleep(0.1)
    else:
        raise BoothError("postgres_start_timeout")
    verify_generation_path(generation_path, gen_fd)
    check_postgres(
        child,
        data,
        socket_dir,
        port,
        admin,
        value["admin_password"],
        pg_bin / "psql",
        generation_path,
    )
    return child, admin


def check_redis(child, *, socket_path: Path, data: Path, port: int, password: str):
    import redis

    local = redis.Redis(
        unix_socket_path=str(socket_path),
        password=password,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    tcp = redis.Redis(
        host="127.0.0.1",
        port=port,
        password=password,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )
    try:
        info = local.info("server")
        config = local.config_get("dir", "port", "bind", "unixsocket", "protected-mode")
        if (
            child.poll() is not None
            or info.get("process_id") != child.pid
            or config
            != {
                "dir": str(data),
                "port": str(port),
                "bind": "127.0.0.1",
                "unixsocket": str(socket_path),
                "protected-mode": "yes",
            }
        ):
            raise BoothError("redis_identity_mismatch")
        remote = tcp.info("server")
        if (
            remote.get("process_id") != child.pid
            or remote.get("run_id") != info.get("run_id")
            or child.poll() is not None
        ):
            raise BoothError("redis_identity_mismatch")
    finally:
        local.close()
        tcp.close()


def start_redis(
    children,
    *,
    gen_fd,
    generation_path: Path,
    port: int,
    value: dict,
    redis_server: Path,
    memory_only=False,
):
    import redis

    if any(char in str(generation_path) for char in ('"', "\\", "\n", "\r")):
        raise BoothError("unsupported_path")
    os.mkdir("redis", 0o700, dir_fd=gen_fd)
    data = generation_path / "redis"
    socket_path = generation_path / "socket" / "redis.sock"
    config = (
        f"bind 127.0.0.1\nport {port}\nprotected-mode yes\n"
        f'requirepass {value["redis_password"]}\ndir "{data}"\n'
        f'unixsocket "{socket_path}"\nunixsocketperm 700\n'
        'daemonize no\nappendonly no\nsave ""\nlogfile ""\n'
    )
    if not memory_only:
        write_private_text(gen_fd, "redis.conf", config)
    verify_generation_path(generation_path, gen_fd)
    redis_command = [
        str(redis_server),
        "-" if memory_only else str(generation_path / "redis.conf"),
    ]
    start_kwargs = {"input_text": config} if memory_only else {}
    child = children.start(
        redis_command,
        env={"PATH": "/usr/bin:/bin", "HOME": str(generation_path)},
        cwd=generation_path,
        **start_kwargs,
    )
    verify_generation_path(generation_path, gen_fd)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ensure_continue()
        if child.poll() is not None:
            raise BoothError("redis_exited")
        try:
            probe = redis.Redis(
                unix_socket_path=str(socket_path),
                password=value["redis_password"],
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            try:
                probe.ping()
            finally:
                probe.close()
            break
        except redis.exceptions.ConnectionError:
            time.sleep(0.1)
    else:
        raise BoothError("redis_start_timeout")
    verify_generation_path(generation_path, gen_fd)
    check_redis(
        child,
        socket_path=socket_path,
        data=data,
        port=port,
        password=value["redis_password"],
    )
    return child


def bootstrap_database(
    *,
    pg_child,
    redis_child,
    gen_fd,
    path: Path,
    generation: str,
    value: dict,
    pg_bin: Path,
    db_port: int,
    redis_port: int,
    web_port: int,
    repo: Path,
    restore_archive=None,
):
    """Bootstrap only after authenticating both foreground servers."""
    home = path / generation
    admin = "booth_admin_" + generation
    env = generation_environment(
        path,
        generation,
        value,
        db_port=db_port,
        redis_port=redis_port,
        web_port=web_port,
    )
    verify_generation_path(home, gen_fd)
    check_postgres(
        pg_child,
        home / "postgres",
        home / "socket",
        db_port,
        admin,
        value["admin_password"],
        pg_bin / "psql",
        home,
    )
    check_redis(
        redis_child,
        socket_path=home / "socket/redis.sock",
        data=home / "redis",
        port=redis_port,
        password=value["redis_password"],
    )
    role = value["role"]
    database = value["database"]
    sql = (
        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        f"NOBYPASSRLS PASSWORD '{value['db_password']}';\n"
        f"CREATE DATABASE {database} OWNER {role};"
    )
    pg_query(
        pg_bin / "psql",
        host=str(home / "socket"),
        port=db_port,
        admin=admin,
        password=value["admin_password"],
        home=home,
        sql=sql,
    )
    query = (
        "SELECT (NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND "
        "NOT rolreplication AND NOT rolbypassrls)::text FROM pg_roles "
        f"WHERE rolname = '{role}';"
    )
    if (
        pg_query(
            pg_bin / "psql",
            host=str(home / "socket"),
            port=db_port,
            admin=admin,
            password=value["admin_password"],
            home=home,
            sql=query,
        )
        != "true"
    ):
        raise BoothError("database_role_mismatch")
    python = repo / ".venv/bin/python"
    api = repo / "apps/api"
    verify_generation_path(home, gen_fd)
    if pg_child.poll() is not None or redis_child.poll() is not None:
        raise BoothError("service_exited")
    if restore_archive is not None:
        restore_archive(gen_fd=gen_fd, home=home, value=value, pg_bin=pg_bin,
                        db_port=db_port, env=env, repo=repo)
    run_private(
        [str(python), "-m", "alembic", "upgrade", "head"], env=env, cwd=api, timeout=120
    )
    verify_generation_path(home, gen_fd)
    if pg_child.poll() is not None or redis_child.poll() is not None:
        raise BoothError("service_exited")
    if restore_archive is not None:
        return env
    seed = (
        "import asyncio\n"
        "from sqlalchemy import select, func\n"
        "from app.config import Settings\n"
        "from app.db import Database\n"
        "from app.models import User\n"
        "from app.routes import seed_personas\n"
        "async def main():\n"
        " s=Settings(); d=Database(s)\n"
        " try:\n"
        "  async with d.transaction() as db:\n"
        "   await seed_personas(db,s)\n"
        "   assert await db.scalar(select(func.count()).select_from(User)) == 5\n"
        " finally: await d.close()\n"
        "asyncio.run(main())\n"
    )
    run_private([str(python), "-c", seed], env=env, cwd=api, timeout=30)
    verify_generation_path(home, gen_fd)
    if pg_child.poll() is not None or redis_child.poll() is not None:
        raise BoothError("service_exited")
    return env


def ensure_children_alive(children):
    if any(child.poll() is not None for child in children.children):
        raise BoothError("service_exited")


def wait_owned_listener(children, child, *, port: int, home: str, cwd: Path):
    lsof = system_lsof()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ensure_continue()
        ensure_children_alive(children)
        try:
            owned = run_private(
                [
                    str(lsof),
                    "-nP",
                    "-a",
                    "-p",
                    str(child.pid),
                    "-iTCP@127.0.0.1:" + str(port),
                    "-sTCP:LISTEN",
                    "-t",
                ],
                env={"PATH": "/usr/bin:/bin", "HOME": home, "LC_ALL": "C"},
                cwd=cwd,
                timeout=3,
            )
            if owned != str(child.pid):
                raise BoothError("listener_mismatch")
            return
        except BoothError as exc:
            if str(exc) != "private_command_failed":
                raise
            time.sleep(0.1)
    raise BoothError("listener_start_timeout")


def local_http(port: int, path: str, *, host: str | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Host": host} if host else {})
        response = connection.getresponse()
        body = response.read(2_000_001)
        if response.status != 200 or len(body) > 2_000_000:
            raise BoothError("http_validation_failed")
        return body
    finally:
        connection.close()


def start_application(children, *, env: dict, api_port: int, repo: Path, workers=True):
    """Start API and replay workers; no browser or Vite process is started."""
    python = repo / ".venv/bin/python"
    api = repo / "apps/api"
    api_child = children.start(
        [
            str(python),
            "-m",
            "uvicorn",
            "app.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(api_port),
            "--no-access-log",
        ],
        env=env,
        cwd=api,
    )
    wait_owned_listener(children, api_child, port=api_port, home=env["HOME"], cwd=api)
    ensure_children_alive(children)
    if json.loads(local_http(api_port, "/ready")).get("status") != "ready":
        raise BoothError("api_not_ready")
    ensure_children_alive(children)
    if workers:
        start_workers(children, env=env, repo=repo)
    return api_child


def start_workers(children, *, env: dict, repo: Path):
    workers = []
    for name in ("worker", "dispatch"):
        workers.append(
            children.start(
                [str(repo / ".venv/bin/python"), "-m", "app.jobs", name],
                env=env,
                cwd=repo / "apps/api",
            )
        )
    return workers


def start_web(
    children,
    *,
    gen_fd: int,
    generation_path: Path,
    web_port: int,
    api_port: int,
    repo: Path,
    llm_mode="replay",
    cache_name="vite-cache",
    public_origin=None,
):
    """Start a dedicated Vite process using only an explicit, secret-free environment."""
    web = (repo / "apps/web").resolve()
    home = generation_path.resolve()
    if home.is_relative_to(web) or web.is_relative_to(home):
        raise BoothError("web_generation_path_overlap")
    verify_generation_path(generation_path, gen_fd)
    reusable_caches = {"vite-cache-booth-public", "vite-cache-booth-replay"}
    if cache_name not in {"vite-cache", "vite-cache-promoted", "vite-cache-replay"} | reusable_caches:
        raise BoothError("invalid_cache_name")
    try:
        os.mkdir(cache_name, 0o700, dir_fd=gen_fd)
    except FileExistsError:
        if cache_name not in reusable_caches:
            raise
    cache_fd = open_private_dir(gen_fd, cache_name)
    try:
        cache_path = generation_path / cache_name
        verify_generation_path(cache_path, cache_fd)
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(generation_path),
            "LC_ALL": "C",
            "NODE_ENV": "production" if public_origin else "development",
            "VITE_APP_MODE": "demo",
            "BARO_WEB_PORT": str(web_port),
            "BARO_API_PORT": str(api_port),
            "BARO_WEB_CACHE": str(cache_path),
        }
        command = [str(service_binary("node")), str(web / "scripts/booth-server.mjs")]
        host = None
        if public_origin:
            env["BARO_PUBLIC_ORIGIN"] = validate_public_origin(public_origin)
            host = urlsplit(env["BARO_PUBLIC_ORIGIN"]).netloc
            run_private([*command, "--build"], env=env, cwd=web, timeout=60)
        child = children.start(
            command,
            env=env,
            cwd=web,
            expected_stop_codes=(0, -signal.SIGTERM, 128 + signal.SIGTERM),
        )
        verify_generation_path(generation_path, gen_fd)
        verify_generation_path(cache_path, cache_fd)
        wait_owned_listener(
            children, child, port=web_port, home=str(generation_path), cwd=web
        )
        if host:
            html = local_http(web_port, "/report", host=host)
            asset = re.search(rb'src="(/assets/[a-zA-Z0-9_.-]+\.js)"', html)
            valid_web = bool(asset and local_http(web_port, asset[1].decode("ascii"), host=host))
            proxy_ready = json.loads(local_http(web_port, "/api/ready", host=host))
            runtime = json.loads(local_http(web_port, "/api/v1/runtime", host=host))
        else:
            html = local_http(web_port, "/")
            module = local_http(web_port, "/src/main.tsx")
            valid_web = b"/src/main.tsx" in html and b"createRoot" in module
            proxy_ready = json.loads(local_http(web_port, "/api/ready"))
            runtime = json.loads(local_http(web_port, "/api/v1/runtime"))
        direct_ready = json.loads(local_http(api_port, "/ready"))
        if (
            b'<div id="root">' not in html
            or not valid_web
            or proxy_ready != direct_ready
            or proxy_ready.get("status") != "ready"
            or runtime.get("mode") != "demo"
            or runtime.get("llm_mode") != llm_mode
            or runtime.get("auth_method") != "demo"
            or runtime.get("mail_delivery") != "disabled"
        ):
            raise BoothError("web_validation_failed")
        ensure_children_alive(children)
        verify_generation_path(generation_path, gen_fd)
        verify_generation_path(cache_path, cache_fd)
        return child
    finally:
        os.close(cache_fd)


def validate_public_origin(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 300:
        raise BoothError("invalid_public_origin")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or any(
        char in value for char in "\\?#"
    ):
        raise BoothError("invalid_public_origin")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise BoothError("invalid_public_origin") from None
    host = url.hostname or ""
    authority = host + (f":{port}" if port is not None else "")
    if (
        url.scheme != "https" or url.path not in {"", "/"} or url.netloc.lower() != authority
        or not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                            r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host)
        or not re.search("[a-z]", host.rsplit(".", 1)[-1])
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise BoothError("invalid_public_origin")
    return "https://" + (host if port in (None, 443) else authority)


def validate_booth_budget(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,6})?", value):
        raise BoothError("invalid_booth_budget")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise BoothError("invalid_booth_budget") from None
    if not 0 < amount <= 5:
        raise BoothError("invalid_booth_budget")
    return value


def live_environment(env, repo, *, public_origin=None, daily_budget_usd="2.00"):
    if __package__:
        from .booth_live_settings import live_overlay
    else:
        from booth_live_settings import live_overlay

    options = {"booth_live": True} if public_origin else {}
    overlay = live_overlay(repo / ".env.service", **options)
    if overlay is None:
        return None
    candidate = {**env, **overlay}
    if public_origin:
        candidate.update(
            APP_BASE_URL=validate_public_origin(public_origin),
            SECURE_COOKIE="true",
            BOOTH_FREE_INPUT_LIVE="true",
            DAILY_LLM_BUDGET_USD=validate_booth_budget(daily_budget_usd),
            LLM_MAX_IMAGES=overlay.get("LLM_MAX_IMAGES", "1"),
        )
        mode_check = "assert s.booth_free_input_live and s.llm_max_images==1\n"
    else:
        candidate["BOOTH_SYNTHETIC_LIVE_ONLY"] = "true"
        mode_check = (
            "assert s.booth_synthetic_live_only and not s.openai_model_vision\n"
            "assert s.llm_chat_reasoning_effort=='none' and s.llm_chat_explicit_cache\n"
        )
    code = (
        "from app.config import Settings\n"
        "from app.services.llm import runtime_mode\n"
        "s=Settings(_env_file=None)\n"
        "assert s.deployment_mode=='demo' and s.mail_delivery=='disabled'\n"
        "assert runtime_mode(s)=='live'\n"
    ) + mode_check
    try:
        run_private(
            [str(repo / ".venv/bin/python"), "-c", code],
            env=candidate,
            cwd=repo / "apps/api",
        )
    except BoothError:
        return None
    return candidate


def require_empty_jobs(env, repo):
    code = (
        "import asyncio\nfrom sqlalchemy import text\n"
        "from app.config import Settings\nfrom app.db import Database\n"
        "async def main():\n s=Settings(_env_file=None); d=Database(s)\n"
        " try:\n  async with d.transaction() as db:\n"
        "   for table in ('processing_jobs','processing_attempts'):\n"
        "    assert (await db.scalar(text('select count(*) from '+table))) == 0\n"
        " finally: await d.close()\nasyncio.run(main())\n"
    )
    run_private(
        [str(repo / ".venv/bin/python"), "-c", code], env=env, cwd=repo / "apps/api"
    )


def require_settled_jobs(env, repo):
    """Keep jobs accepted in replay from crossing into a newly opened live mode."""
    code = (
        "import asyncio\nfrom sqlalchemy import text\n"
        "from app.config import Settings\nfrom app.db import Database\n"
        "async def main():\n d=Database(Settings(_env_file=None))\n"
        " try:\n  async with d.transaction() as db:\n"
        "   q=\"SELECT EXISTS (SELECT 1 FROM processing_jobs WHERE state IN "
        "('queued', 'running', 'retry_wait'))\"\n"
        "   assert not await db.scalar(text(q))\n"
        " finally: await d.close()\nasyncio.run(main())\n"
    )
    run_private(
        [str(repo / ".venv/bin/python"), "-c", code], env=env, cwd=repo / "apps/api"
    )


def require_replay_drained(env, repo, *, live_history=False):
    mode = "live" if live_history else "replay"
    code = (
        "import asyncio\nfrom sqlalchemy import text\n"
        "from app.config import Settings\nfrom app.db import Database\n"
        "async def main():\n s=Settings(_env_file=None); assert s.llm_fake; d=Database(s)\n"
        " try:\n  async with d.transaction() as db:\n"
        "   assert (await db.scalar(text(\"select count(*) from processing_jobs where state != 'succeeded'\"))) == 0\n"
        f"   assert (await db.scalar(text(\"select count(*) from processing_attempts where mode != '{mode}'\"))) == 0\n"
        " finally: await d.close()\nasyncio.run(main())\n"
    )
    run_private(
        [str(repo / ".venv/bin/python"), "-c", code], env=env, cwd=repo / "apps/api"
    )


def live_showcase_metrics(env, repo):
    code = (
        "import asyncio, json\nfrom sqlalchemy import text\n"
        "from app.config import Settings\nfrom app.db import Database\n"
        "async def main():\n s=Settings(_env_file=None); assert s.booth_synthetic_live_only; d=Database(s)\n"
        " try:\n  async with d.transaction() as db:\n"
        "   await db.execute(text('SET TRANSACTION READ ONLY'))\n"
        "   rows=await db.execute(text(\"select j.kind, a.error_code, a.applied, a.metadata_json "
        "from processing_attempts a join processing_jobs j on j.id=a.job_id where a.mode='live'\"))\n"
        "   keys=('model','sent','cost_usd','reserved_usd','reservation_day','duration_ms',"
        "'input_tokens','cached_tokens','output_tokens','cache_hit','image_count','attempts')\n"
        "   print(json.dumps([dict(kind=k,error=e,applied=a,**{key:m.get(key) for key in keys}) "
        "for k,e,a,m in rows]))\n"
        " finally: await d.close()\nasyncio.run(main())\n"
    )
    return json.loads(
        run_private([str(repo / ".venv/bin/python"), "-c", code], env=env, cwd=repo / "apps/api")
    )


def seed_fingerprint(env, repo):
    code = (
        "import asyncio\nfrom sqlalchemy import text\n"
        "from app.config import Settings\nfrom app.db import Database\n"
        "async def main():\n s=Settings(_env_file=None); assert s.llm_fake; d=Database(s)\n"
        " try:\n  async with d.transaction() as db:\n"
        "   for t in ('tickets','ticket_events','assignments','completion_reports',"
        "'uploads','image_variants','image_approvals','ticket_merges',"
        "'processing_jobs','processing_attempts','information_requests'):\n"
        "    q=\"select md5(coalesce(string_agg(to_jsonb(x)::text, '' order by id),'')) from \"+t+\" x\"\n"
        "    print(await db.scalar(text(q)))\n"
        " finally: await d.close()\nasyncio.run(main())\n"
    )
    return run_private(
        [str(repo / ".venv/bin/python"), "-c", code], env=env, cwd=repo / "apps/api"
    )


def storage_fingerprint(home: Path):
    storage = home / "storage"
    digest = hashlib.sha256()
    for root, dirs, files in os.walk(storage, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(root) / name
            if path.is_symlink():
                raise BoothError("storage_symlink")
        for name in sorted(files):
            path = Path(root) / name
            if not path.is_file():
                raise BoothError("storage_not_regular")
            relative = str(path.relative_to(storage)).encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()



def promotion_ports_free(api_port: int, web_port: int) -> bool:
    """Probe only the proposed loopback endpoints; never stop an existing listener."""
    sockets = []
    try:
        if (type(api_port) is not int or type(web_port) is not int
                or not 1024 <= api_port <= 65535 or not 1024 <= web_port <= 65535
                or api_port == web_port):
            raise BoothError("invalid_port")
        for port in (api_port, web_port):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            try:
                # TIME_WAIT from the stopped listener must not block a new listener.
                # listen() also proves an existing live listener cannot be displaced.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                sock.listen(1)
            except OSError:
                return False
        return True
    finally:
        for sock in sockets:
            sock.close()


def promote_showcase(children, app_children, *, env, repo, gen_fd, home,
                     api_port, web_port, fingerprint, photo_fingerprint, live_history=False):
    """Move owned foreground app processes only; keep the same DB and storage.

    Call only after the operator has confirmed the old fixed endpoints stopped.
    Probe and fingerprint failures leave the candidate processes running. Once
    shutdown starts, a later failure closes owned children and requires recovery.
    """
    ensure_children_alive(children)
    verify_generation_path(home, gen_fd)
    if not promotion_ports_free(api_port, web_port):
        print("PROMOTION_PORTS_UNAVAILABLE", flush=True)
        return False
    require_replay_drained(env, repo, live_history=live_history)
    if (seed_fingerprint(env, repo) != fingerprint
            or storage_fingerprint(home) != photo_fingerprint):
        print("PROMOTION_DATA_CHANGED", flush=True)
        return False
    for child in reversed(app_children):
        children.stop(child)
    promoted_env = {**env, "APP_BASE_URL": f"http://127.0.0.1:{web_port}"}
    start_application(children, env=promoted_env, api_port=api_port, repo=repo, workers=True)
    start_web(children, gen_fd=gen_fd, generation_path=home, web_port=web_port,
              api_port=api_port, repo=repo, llm_mode="replay", cache_name="vite-cache-promoted")
    require_replay_drained(promoted_env, repo, live_history=live_history)
    if (seed_fingerprint(promoted_env, repo) != fingerprint
            or storage_fingerprint(home) != photo_fingerprint):
        raise BoothError("promotion_fingerprint_changed")
    return True


def switch_showcase_mode(children, infrastructure, *, env, replay_env, repo, gen_fd, home,
                         api_port, web_port, public_origin=None, daily_budget_usd="2.00"):
    """Restart only the application processes while preserving the database, queue and files."""
    verify_generation_path(home, gen_fd)
    loopback = {
        **replay_env, "APP_BASE_URL": f"http://127.0.0.1:{web_port}", "SECURE_COOKIE": "false",
    }
    if public_origin:
        desired = live_environment(
            loopback, repo, public_origin=public_origin, daily_budget_usd=daily_budget_usd,
        )
        if desired is None:
            raise BoothError("live_configuration_unavailable")
    else:
        desired = loopback

    def stop_app():
        owned = (child for child in reversed(children.children) if child not in infrastructure)
        for child in list(owned):
            children.stop(child, allow_exited=True)

    def start_app(settings, *, origin=None):
        start_application(children, env=settings, api_port=api_port, repo=repo, workers=False)
        start_web(
            children, gen_fd=gen_fd, generation_path=home, web_port=web_port, api_port=api_port,
            repo=repo, public_origin=origin, llm_mode="live" if origin else "replay",
            cache_name="vite-cache-booth-public" if origin else "vite-cache-booth-replay",
        )
        start_workers(children, env=settings, repo=repo)
        ensure_children_alive(children)

    def status():
        if public_origin is None:
            return "BOOTH_MODE replay"
        runtime = json.loads(local_http(api_port, "/v1/runtime"))
        photo = "available" if runtime.get("photo_ai_capability") == "available" else "manual"
        return f"BOOTH_MODE live photo={photo}"

    if desired == env:
        return env, status()

    # Quiesce HTTP writes and workers before admitting the live policy.
    stop_app()
    try:
        if public_origin:
            require_settled_jobs(loopback, repo)
        start_app(desired, origin=public_origin)
        return desired, status()
    except (BoothError, OSError, subprocess.TimeoutExpired, ValueError):
        stop_app()
        # Always restore replay on a failed live transition, preserving the running generation.
        start_app(loopback)
        return loopback, "BOOTH_MODE_REFUSED_REPLAY"


def supervise(
    path: Path,
    generation: str,
    *,
    campus=False,
    live_ai=False,
    manual_ai=False,
    campus_profile=None,
    memory_value=None,
    fixed_ports=False,
    seed_scenarios=False,
    seed_live_history=False,
    seed_showcase=False,
    promotable_showcase=False,
    restore_archive=None,
):
    """Run one generation until interrupted; a claimed generation cannot be restarted."""
    global promotion_ready, promotion_requested
    if restore_archive is not None and (
        not campus or memory_value is None or campus_profile is not None or live_ai or manual_ai
        or seed_scenarios or seed_live_history or seed_showcase or promotable_showcase
    ):
        raise BoothError("invalid_arguments")
    if restore_archive is not None and fixed_ports and not promotion_ports_free(8011, 5181):
        raise BoothError("fixed_ports_in_use")
    if seed_showcase and live_ai and not promotable_showcase:
        raise BoothError("invalid_arguments")
    if promotable_showcase and (not seed_showcase or fixed_ports or manual_ai
                                or campus_profile not in ("r10", "r11")):
        raise BoothError("invalid_arguments")
    if campus_profile is not None:
        if not campus or campus_profile not in ("r08", "r10", "r11"):
            raise BoothError("invalid_arguments")
        if __package__:
            from .prepare_demo_campus import profile_spec, workbook_candidate
        else:
            from prepare_demo_campus import profile_spec, workbook_candidate
        profile_path = profile_spec(campus_profile)[0]
        workbook_candidate(profile_path.read_bytes(), campus_profile)
    pg_bin = postgres_bin()
    redis_bin = service_binary("redis-server")
    system_lsof()
    service_binary("node")
    repo = Path(__file__).resolve().parents[1]
    if not (
        executable_file(repo / ".venv/bin/python")
        and (repo / "apps/web/scripts/booth-server.mjs").is_file()
    ):
        raise BoothError("required_tool_missing")
    if any(char in str(path) for char in ('"', "\\", "\n", "\r")):
        raise BoothError("unsupported_path")
    ports = {5181, 8011} if fixed_ports else set()
    while len(ports) < 4:
        port = free_port()
        if (not promotable_showcase and restore_archive is None
                or port not in {5174, 5181, 8001, 8011}):
            ports.add(port)
    if fixed_ports:
        db_port, redis_port = tuple(ports - {5181, 8011})
        web_port, api_port = 5181, 8011
    else:
        db_port, redis_port, web_port, api_port = tuple(ports)
    gen_fd, value = claim_generation(path, generation, memory_value=memory_value)
    children = OwnedChildren()
    requested_stop = False
    effective_live = False
    rebase_listener = None
    try:
        ensure_continue()
        home = path / generation
        private_kwargs = {"memory_only": True} if memory_value is not None else {}
        pg, _ = start_postgres(
            children,
            gen_fd=gen_fd,
            generation_path=home,
            port=db_port,
            value=value,
            pg_bin=pg_bin,
            **private_kwargs,
        )
        redis_child = start_redis(
            children,
            gen_fd=gen_fd,
            generation_path=home,
            port=redis_port,
            value=value,
            redis_server=redis_bin,
            **private_kwargs,
        )
        env = bootstrap_database(
            pg_child=pg,
            redis_child=redis_child,
            gen_fd=gen_fd,
            path=path,
            generation=generation,
            value=value,
            pg_bin=pg_bin,
            db_port=db_port,
            redis_port=redis_port,
            web_port=web_port,
            repo=repo,
            **({"restore_archive": restore_archive} if restore_archive is not None else {}),
        )
        if campus:
            env["PLANNING_DATASET_PROVIDER"] = "db"
            env["BARO_CAMPUS_OWNED_SUPERVISOR"] = "1"
        verify_generation_path(home, gen_fd)
        infrastructure = set(children.children) if seed_showcase else set()
        replay_env = env
        api_child = start_application(
            children, env=env, api_port=api_port, repo=repo,
            workers=not campus or restore_archive is not None,
        )
        if campus and restore_archive is None:
            ensure_children_alive(children)
            verify_generation_path(home, gen_fd)
            run_private(
                [
                    str(repo / ".venv/bin/python"),
                    str(repo / "scripts/prepare_demo_campus.py"),
                    str(api_port),
                ]
                + ([campus_profile] if campus_profile else []),
                env=env,
                cwd=repo,
                timeout=180,
            )
            ensure_children_alive(children)
            verify_generation_path(home, gen_fd)
            require_empty_jobs(env, repo)
            if seed_scenarios:
                history_env = {**env, "BARO_BOOTH_HISTORY_REPLAY": "1"}
                replay_workers = start_workers(children, env=history_env, repo=repo)
                history = [
                    str(repo / ".venv/bin/python"),
                    str(repo / "scripts/seed_booth_scenarios.py"),
                    "--api-port",
                    str(api_port),
                    "--generation",
                    generation,
                    "--allow-replay",
                    "--history-only",
                ]
                output = run_private(
                    history, env=history_env, cwd=repo, timeout=45 * 60
                )
                if "HISTORY_MERGE_READY" not in output.splitlines():
                    raise BoothError("history_merge_missing")
                require_replay_drained(env, repo)
                fingerprint = seed_fingerprint(env, repo)
                output = run_private(
                    history, env=history_env, cwd=repo, timeout=45 * 60
                )
                if "HISTORY_MERGE_READY" not in output.splitlines():
                    raise BoothError("history_merge_missing")
                require_replay_drained(env, repo)
                if seed_fingerprint(env, repo) != fingerprint:
                    raise BoothError("history_seed_changed_on_repeat")
                print("HISTORY_MERGE_READY", flush=True)
                for child in reversed(replay_workers):
                    children.stop(child)
                require_replay_drained(env, repo)
            candidate = live_environment(env, repo) if live_ai else None
            if candidate is not None:
                children.stop(api_child)
                env = candidate
                api_child = start_application(
                    children, env=env, api_port=api_port, repo=repo, workers=False
                )
                runtime = json.loads(local_http(api_port, "/v1/runtime"))
                if runtime.get("llm_mode") != "live":
                    raise BoothError("live_runtime_unavailable")
                if not seed_scenarios:
                    require_empty_jobs(env, repo)
                effective_live = True
            if not (manual_ai and effective_live):
                start_workers(children, env=env, repo=repo)
            if seed_live_history and not effective_live:
                raise BoothError("live_history_requires_live")
            if seed_showcase and live_ai and not effective_live:
                raise BoothError("live_showcase_requires_live")
            if live_ai and candidate is None:
                print("LIVE_UNAVAILABLE_REPLAY", flush=True)
        start_web(
            children,
            gen_fd=gen_fd,
            generation_path=home,
            web_port=web_port,
            api_port=api_port,
            repo=repo,
            llm_mode="live" if env.get("LLM_FAKE") == "false" else "replay",
        )
        ensure_children_alive(children)
        verify_generation_path(home, gen_fd)
        ensure_continue()
        if seed_scenarios or seed_live_history or seed_showcase:
            command = [
                str(repo / ".venv/bin/python"),
                str(repo / "scripts/seed_booth_scenarios.py"),
                "--api-port",
                str(api_port),
                "--generation",
                generation,
            ]
            if seed_live_history:
                command.append("--live-history-only")
            if seed_showcase:
                command.append("--showcase-only")
            if not effective_live:
                command.append("--allow-replay")
            seed_started = time.monotonic()
            try:
                run_private(command, env=env, cwd=repo, timeout=45 * 60,
                            seed_diagnostics=seed_live_history or seed_showcase)
            finally:
                if seed_showcase and effective_live:
                    write_private(gen_fd, "live-showcase-metrics.json", {
                        "elapsed_s": round(time.monotonic() - seed_started, 3),
                        "attempts": live_showcase_metrics(env, repo),
                    })
            if seed_showcase and effective_live:
                require_replay_drained(replay_env, repo, live_history=True)
                fingerprint = seed_fingerprint(replay_env, repo)
                photo_fingerprint = storage_fingerprint(home)
                for child in reversed([p for p in children.children if p not in infrastructure]):
                    children.stop(child)
                env = replay_env
                start_application(children, env=env, api_port=api_port, repo=repo)
                start_web(
                    children, gen_fd=gen_fd, generation_path=home, web_port=web_port,
                    api_port=api_port, repo=repo, llm_mode="replay", cache_name="vite-cache-replay",
                )
                require_replay_drained(env, repo, live_history=True)
                if (seed_fingerprint(env, repo) != fingerprint
                        or storage_fingerprint(home) != photo_fingerprint):
                    raise BoothError("scenario_seed_changed_on_replay_switch")
            if not effective_live:
                require_replay_drained(env, repo)
                fingerprint = seed_fingerprint(env, repo)
                photo_fingerprint = storage_fingerprint(home)
                run_private(command, env=env, cwd=repo, timeout=45 * 60)
                require_replay_drained(env, repo)
                if (
                    seed_fingerprint(env, repo) != fingerprint
                    or storage_fingerprint(home) != photo_fingerprint
                ):
                    raise BoothError("scenario_seed_changed_on_repeat")
            ensure_children_alive(children)
            verify_generation_path(home, gen_fd)
        write_private(
            gen_fd,
            "runtime.json",
            {
                "api_port": api_port,
                "web_port": web_port,
                "database_port": db_port,
                "redis_port": redis_port,
            },
        )
        os.fsync(gen_fd)
        ensure_continue()
        if (seed_showcase and not promotable_showcase) or restore_archive is not None:
            rebase_listener = open_showcase_rebase(home, gen_fd)
        print("RUNNING", flush=True)
        if promotable_showcase:
            app_children = [child for child in children.children if child not in infrastructure]
            write_private(gen_fd, "promotion-ready.json", {
                "pid": os.getpid(), "generation": generation,
                "candidate_api_port": api_port, "candidate_web_port": web_port,
                "target_api_port": 8011, "target_web_port": 5181,
            })
            os.fsync(gen_fd)
            promotion_ready = True
            print("PROMOTION_READY", flush=True)
        def switch_mode(settings, **options):
            return switch_showcase_mode(
                children, infrastructure, env=settings, replay_env=replay_env,
                repo=repo, gen_fd=gen_fd, home=home, api_port=api_port, web_port=web_port,
                **options,
            )

        while True:
            ensure_children_alive(children)
            if shutdown_requested:
                requested_stop = True
                break
            if promotable_showcase and promotion_requested:
                promotion_requested = False
                promotion_ready = False
                promoted = promote_showcase(
                    children, app_children, env=env, repo=repo, gen_fd=gen_fd, home=home,
                    api_port=8011, web_port=5181, fingerprint=fingerprint,
                    photo_fingerprint=photo_fingerprint, live_history=seed_showcase and effective_live,
                )
                if promoted:
                    promotion_ready = False
                    write_private(gen_fd, "promoted.json", {
                        "pid": os.getpid(), "generation": generation,
                        "api_port": 8011, "web_port": 5181,
                        "database_port": db_port, "redis_port": redis_port,
                    })
                    os.fsync(gen_fd)
                    api_port = 8011
                    web_port = 5181
                    env = {**env, "APP_BASE_URL": f"http://127.0.0.1:{web_port}"}
                    rebase_listener = open_showcase_rebase(home, gen_fd)
                    print("PROMOTED", flush=True)
                else:
                    promotion_ready = True
                    print("PROMOTION_REFUSED", flush=True)
            if rebase_listener is not None:
                env = serve_showcase_rebase(
                    rebase_listener, home=home, gen_fd=gen_fd, env=env, repo=repo,
                    generation=generation, api_port=api_port,
                    switch_mode=switch_mode if restore_archive is None else None,
                )
            time.sleep(0.5)
    finally:
        try:
            try:
                if rebase_listener is not None:
                    close_showcase_rebase(rebase_listener, gen_fd)
            finally:
                children.close()
                if requested_stop:
                    write_private(gen_fd, "stopped.json", {"state": "stopped"})
                    os.fsync(gen_fd)
        finally:
            os.close(gen_fd)


def main():
    global shutdown_requested, promotion_ready, promotion_requested
    shutdown_requested = False
    promotion_ready = promotion_requested = False
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "prepare", "run", "run-new", "rebase", "mode"))
    parser.add_argument("path", type=Path)
    parser.add_argument("generation", nargs="?")
    parser.add_argument("--campus", action="store_true")
    parser.add_argument("--fixed-ports", action="store_true")
    parser.add_argument("--campus-profile", choices=("r08", "r10", "r11"), help=argparse.SUPPRESS)
    parser.add_argument("--seed-scenarios", action="store_true")
    parser.add_argument("--seed-live-history", action="store_true")
    parser.add_argument(
        "--seed-showcase", action="store_true",
        help="Load the bundled campus catalog and synthetic cases; requires --campus",
    )
    parser.add_argument("--promotable-showcase", action="store_true")
    parser.add_argument("--live-ai", action="store_true")
    parser.add_argument("--manual-ai", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--booth-live", action="store_true", help="Enable live AI for the running booth")
    mode.add_argument("--replay", action="store_true", help="Return the running booth to loopback replay")
    parser.add_argument("--public-origin", help="Booth HTTPS origin for --booth-live")
    parser.add_argument("--daily-budget-usd", help="Booth live daily USD cap (default 2.00, maximum 5.00)")
    args = parser.parse_args()
    try:
        mode_flags = args.booth_live or args.replay or (
            args.public_origin is not None or args.daily_budget_usd is not None
        )
        if args.command in {"rebase", "mode"}:
            if not args.generation or args.campus or args.fixed_ports or args.campus_profile or (
                args.seed_scenarios or args.seed_live_history or args.seed_showcase
                or args.promotable_showcase or args.live_ai or args.manual_ai
            ):
                raise BoothError("invalid_arguments")
            if args.command == "rebase" and not mode_flags:
                print(request_showcase_rebase(args.path, args.generation))
            elif args.command == "mode" and args.booth_live and args.public_origin:
                print(request_showcase_mode(
                    args.path, args.generation, public_origin=args.public_origin,
                    daily_budget_usd=args.daily_budget_usd if args.daily_budget_usd is not None else "2.00",
                ))
            elif args.command == "mode" and args.replay and (
                args.public_origin is None and args.daily_budget_usd is None
            ):
                print(request_showcase_mode(args.path, args.generation))
            else:
                raise BoothError("invalid_arguments")
            return 0
        if mode_flags:
            raise BoothError("invalid_arguments")
        if args.promotable_showcase and not args.seed_showcase:
            raise BoothError("invalid_arguments")
        if args.seed_showcase and args.campus_profile is None:
            args.campus_profile = SHOWCASE_CAMPUS_PROFILE
        if args.seed_showcase and (args.command != "run-new" or not args.campus
                or args.campus_profile not in ("r10", "r11") or args.fixed_ports
                or (args.live_ai and not args.promotable_showcase)
                or args.manual_ai or args.seed_scenarios or args.seed_live_history):
            raise BoothError("invalid_arguments")
        if args.fixed_ports and args.command != "run-new":
            raise BoothError("invalid_arguments")
        if args.seed_live_history and (
            args.command != "run-new"
            or not args.campus
            or not args.live_ai
            or args.manual_ai
            or args.seed_scenarios
        ):
            raise BoothError("invalid_arguments")
        if args.seed_scenarios and (args.command != "run-new" or args.manual_ai):
            raise BoothError("invalid_arguments")
        if args.command == "run-new" and (
            args.generation is not None
            or not args.campus
            or args.campus_profile not in (None, "r08", "r10", "r11")
        ):
            raise BoothError("invalid_arguments")
        if args.campus_profile and (
            args.command not in ("run", "run-new") or not args.campus
        ):
            raise BoothError("invalid_arguments")
        if args.live_ai and (args.command not in ("run", "run-new") or not args.campus):
            raise BoothError("invalid_arguments")
        if args.manual_ai and not args.live_ai:
            raise BoothError("invalid_arguments")
        if args.command == "init":
            if args.generation is not None or args.campus:
                raise BoothError("invalid_arguments")
            initialize(args.path)
            print("INITIALIZED")
        elif args.command == "prepare":
            if args.generation is not None or args.campus:
                raise BoothError("invalid_arguments")
            print("PREPARED_ONLY", prepare_generation(args.path))
        else:
            if args.command == "run-new":
                generation, memory_value = prepare_generation(
                    args.path, memory_only=True
                )
            else:
                if not args.generation:
                    raise BoothError("invalid_arguments")
                generation, memory_value = args.generation, None
            previous_term = signal.getsignal(signal.SIGTERM)
            previous_int = signal.getsignal(signal.SIGINT)
            previous_usr1 = signal.getsignal(signal.SIGUSR1)
            try:
                signal.signal(signal.SIGTERM, request_shutdown)
                signal.signal(signal.SIGINT, request_shutdown)
                if args.promotable_showcase:
                    signal.signal(signal.SIGUSR1, request_promotion)
                if args.campus:
                    kwargs = {
                        "campus": True,
                        "live_ai": args.live_ai,
                        "manual_ai": args.manual_ai,
                    }
                    if args.campus_profile:
                        kwargs["campus_profile"] = args.campus_profile
                    if memory_value is not None:
                        kwargs.update(
                            memory_value=memory_value,
                            fixed_ports=args.fixed_ports,
                            campus_profile=args.campus_profile or "r08",
                        )
                    if args.seed_scenarios:
                        kwargs["seed_scenarios"] = True
                    if args.seed_live_history:
                        kwargs["seed_live_history"] = True
                    if args.seed_showcase:
                        kwargs["seed_showcase"] = True
                    if args.promotable_showcase:
                        kwargs["promotable_showcase"] = True
                    supervise(args.path, generation, **kwargs)
                else:
                    supervise(args.path, generation)
            finally:
                signal.signal(signal.SIGTERM, previous_term)
                signal.signal(signal.SIGINT, previous_int)
                if args.promotable_showcase:
                    signal.signal(signal.SIGUSR1, previous_usr1)
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 -- errors may contain private credentials
        print("REFUSED_OR_INCOMPLETE", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

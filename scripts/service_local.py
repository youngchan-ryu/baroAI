"""Separate local service runtime; never resets demo data or prints credentials."""
from __future__ import annotations

import argparse
import base64
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import dev

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / '.local/service'
PRIVATE = BASE / 'credentials.json'
WEB_PORT = 5174
API_PORT = 8001
REDIS_PORT = 56380
PUBLIC_READY = f'Public service ready: http://127.0.0.1:{WEB_PORT}/api/ready'


class PublicServiceError(RuntimeError):
    """Fixed, operator-facing diagnostic with no configuration values."""


def private_dir(path: Path):
    relative = path.relative_to(ROOT)
    current = ROOT
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('Symbolic link in service directory')
        current.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Service directory must be private and owned')


def private_file(path: Path, content: str | None = None):
    if path.is_symlink():
        raise RuntimeError('Symbolic link in service settings')
    if not path.exists() and content is not None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
    if not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Service settings must be private and owned')
    return path.read_text()


def credentials():
    private_dir(BASE)
    values = {key: secrets.token_urlsafe(48) for key in ('dev', 'test', 'jwt_dev', 'jwt_test', 'otp_dev', 'otp_test')}
    saved = json.loads(private_file(PRIVATE, json.dumps(values)))
    if set(saved) != set(values) or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{40,100}', v) for v in saved.values()):
        raise RuntimeError('Invalid service credentials')
    return saved


def mail_key_from(content: str, setting_name='MAIL_ENCRYPTION_KEY'):
    """Read one existing key without displaying or silently replacing it."""
    if setting_name not in {'MAIL_ENCRYPTION_KEY', 'EXPORT_ENCRYPTION_KEY'}:
        raise RuntimeError('Unsupported encryption setting')
    from dotenv.parser import parse_stream
    from io import StringIO
    bindings = list(parse_stream(StringIO(content)))
    if any(binding.error for binding in bindings):
        raise RuntimeError('Invalid service settings syntax; preserved')
    matching = [binding for binding in bindings if binding.key == setting_name]
    if len(matching) > 1:
        raise RuntimeError('Duplicate mail encryption settings')
    if not matching:
        return None
    value = matching[0].value
    if not value or not re.fullmatch(r'[A-Za-z0-9_-]{43}=', value):
        raise RuntimeError('Invalid existing mail encryption key; preserved')
    if len(base64.urlsafe_b64decode(value)) != 32:
        raise RuntimeError('Invalid existing mail encryption key; preserved')
    return value


def add_mail_key(target: Path, setting_name='MAIL_ENCRYPTION_KEY'):
    """Add once under an owned lock; keep every existing byte and never rotate."""
    private_dir(BASE)
    lock_path = BASE / 'mail-configure.lock'
    private_file(lock_path, '')
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        before = private_file(target)
        if mail_key_from(before, setting_name):
            return False
        key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
        content = before + ('' if before.endswith('\n') else '\n') + f'{setting_name}={key}\n'
        descriptor, name = tempfile.mkstemp(prefix='.mail-configure-', dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Refuse an edit made since the original read instead of overwriting it.
            if private_file(target) != before:
                raise RuntimeError('Service settings changed during preparation')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return True


def configure_mail():
    targets = [ROOT / '.env.service', ROOT / '.env.service.test']
    # Validate both before adding anything; repeated runs preserve each generated key.
    for target in targets:
        mail_key_from(private_file(target))
    for target in targets:
        add_mail_key(target)
    print('Separate mail encryption keys prepared; existing keys and delivery settings preserved.')


def configure_exports():
    targets = [ROOT / name for name in ('.env', '.env.service', '.env.service.test')]
    for target in targets:
        mail_key_from(private_file(target), 'EXPORT_ENCRYPTION_KEY')
    for target in targets:
        add_mail_key(target, 'EXPORT_ENCRYPTION_KEY')
    print('Separate export encryption keys prepared; existing data and settings preserved.')


def redis_runtime():
    # A separate module instance keeps the existing tested lifecycle and demo globals intact.
    import importlib.util
    spec = importlib.util.spec_from_file_location('baro_service_redis', ROOT / 'scripts/redis_local.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PORT = REDIS_PORT
    runtime_root = BASE / 'runtime'
    private_dir(runtime_root)
    return module.LocalRedis(runtime_root)


def configure():
    dev.owned()
    if not dev.running():
        raise RuntimeError('Start the owned project PostgreSQL first')
    values = credentials()
    redis = redis_runtime()
    redis.initialize()
    password = redis.password()
    for scope in ('dev', 'test'):
        database = role = f'baro_service_{scope}'
        dev.sql("SELECT 'CREATE ROLE " + role + " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD ''" + values[scope] + "''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='" + role + "')\\gexec\n")
        dev.sql(f"SELECT 'CREATE DATABASE {database} OWNER {role}' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{database}')\\gexec\n")
        dev.sql(f'REVOKE CONNECT ON DATABASE {database} FROM PUBLIC; GRANT CONNECT ON DATABASE {database} TO {role};')
        dev.sql('REVOKE CREATE ON SCHEMA public FROM PUBLIC;', database)
        for name in ('uploads', 'mail'):
            private_dir(BASE / scope / name)
        values_env = {
            'DEPLOYMENT_MODE': 'service', 'SERVICE_NAMESPACE': 'baro_service',
            'APP_ENV': scope, 'DEMO_AUTH': 'false', 'LLM_FAKE': 'true', 'AUTO_CONFIRM': 'false',
            'DATABASE_URL': f'postgresql+asyncpg://{role}:{values[scope]}@127.0.0.1:55432/{database}',
            'REDIS_URL': f'redis://:{password}@127.0.0.1:56380/{2 if scope == "dev" else 3}',
            'APP_BASE_URL': 'http://127.0.0.1:5174', 'JWT_SECRET': values[f'jwt_{scope}'],
            'OTP_PEPPER': values[f'otp_{scope}'], 'MAIL_DELIVERY': 'local',
            'ALLOWED_EMAIL_DOMAIN': 'snu.ac.kr', 'SECURE_COOKIE': 'false',
            'STORAGE_PATH': str(BASE / scope / 'uploads'), 'MAIL_SINK_PATH': str(BASE / scope / 'mail'),
        }
        target = ROOT / ('.env.service' if scope == 'dev' else '.env.service.test')
        content = ''.join(f'{key}={value}\n' for key, value in values_env.items())
        actual = private_file(target, content)
        # The independently added mail key is not part of the original six credentials.
        comparable = actual
        for setting in ('MAIL_ENCRYPTION_KEY', 'EXPORT_ENCRYPTION_KEY'):
            if mail_key_from(actual, setting):
                comparable = re.sub(r'^' + setting + r'=[^\n]*\n?', '', comparable, flags=re.MULTILINE)
        if comparable != content:
            raise RuntimeError('Existing service settings differ; preserved without overwrite')
    configure_mail()
    # Restrict access between these local application roles, preserving the demo owner.
    for database in ('baro_dev', 'baro_test'):
        dev.sql(f'REVOKE CONNECT ON DATABASE {database} FROM PUBLIC; GRANT CONNECT ON DATABASE {database} TO baro;')
    print('Separate service databases, roles and private configuration ready; demo data preserved.')


def environment(scope, *, public_origin=None):
    target = ROOT / ('.env.service' if scope == 'dev' else '.env.service.test')
    from dotenv import dotenv_values
    private_file(target)
    saved = dotenv_values(target, interpolate=False)
    if not saved or any(not isinstance(v, str) for v in saved.values()):
        raise RuntimeError('Invalid service environment')
    sys.path.insert(0, str(ROOT / 'apps/api'))
    from app.config import Settings
    values = {k.lower(): v for k, v in saved.items()}
    if public_origin is not None:
        values['service_public_origin'] = public_origin
        # Validate with the same defaults as the isolated child, ignoring caller settings.
        for name, field in Settings.model_fields.items():
            if name not in values and not field.is_required():
                values[name] = field.get_default(call_default_factory=True)
    if 'llm_price_table' in values:
        try:
            parsed = json.loads(values['llm_price_table'])
        except (ValueError, TypeError):
            raise RuntimeError('Invalid service price table') from None
        if not isinstance(parsed, dict):
            raise RuntimeError('Invalid service price table')
        values['llm_price_table'] = parsed
    config = Settings(_env_file=None, **values)
    if config.deployment_mode != 'service' or config.app_env != scope:
        raise RuntimeError('Service scope mismatch')
    if public_origin is not None:
        from app.services.llm import runtime_mode
        from sqlalchemy.engine import make_url

        postgres = make_url(config.database_url.get_secret_value())
        redis = make_url(config.redis_url.get_secret_value())
        if postgres.port != int(dev.PORT) or redis.port != REDIS_PORT:
            raise PublicServiceError('Public service requires the existing project PostgreSQL and service Redis ports.')
        if runtime_mode(config) != 'live' or config.mail_delivery != 'gmail':
            raise PublicServiceError('Public service requires the configured live AI and Gmail service settings.')
        return {
            **{name: value for name, value in os.environ.items() if name in {'PATH', 'HOME', 'LANG', 'LC_ALL'}},
            **{name.upper(): value for name, value in saved.items() if name.lower() in Settings.model_fields},
            'BARO_ISOLATED_SETTINGS': '1',
            'SERVICE_PUBLIC_ORIGIN': public_origin,
        }
    # Validated file values take precedence over inherited settings and default .env.
    return {**os.environ, **saved}


def read_public_origin():
    private_dir(BASE)
    from app.config import booth_https_origin

    try:
        fd = os.open(BASE / 'public-origin', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise PublicServiceError('Set the private .local/service/public-origin file before starting public service.') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PublicServiceError('The public-origin file must be a private, owner-only file.')
        content = stream.read(305)
    try:
        origin = content.removesuffix(b'\n').decode('ascii')
    except UnicodeError:
        origin = ''
    if not booth_https_origin(origin):
        raise PublicServiceError('The public-origin file must contain one canonical HTTPS origin.')
    return origin


def require_open_ports():
    for port in (WEB_PORT, API_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
                probe.listen(1)
            except OSError:
                raise PublicServiceError(f'Public service port {port} is unavailable. Stop the existing listener before retrying.') from None


def api_command():
    return [sys.executable, '-m', 'uvicorn', 'app.main:create_app', '--factory', '--host', '127.0.0.1',
            '--port', str(API_PORT), '--no-access-log', '--no-proxy-headers']


def local_http(port, path, *, host):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', path, headers={'Host': host})
        response = connection.getresponse()
        body = response.read(2_000_001)
        if response.status != 200 or len(body) > 2_000_000:
            raise ValueError('public_service_probe_failed')
        return body
    finally:
        connection.close()


def verify_public_web(host):
    loopback = f'127.0.0.1:{WEB_PORT}'
    ready = {'status': 'ready', 'database': 'ok'}
    if any(json.loads(local_http(WEB_PORT, '/api/ready', host=name)) != ready for name in (host, loopback)):
        raise ValueError('public_service_database_not_ready')
    runtime = json.loads(local_http(WEB_PORT, '/api/v1/runtime', host=host))
    if (runtime.get('mode') != 'service' or runtime.get('auth_method') != 'email_otp' or
            runtime.get('llm_mode') != 'live' or runtime.get('mail_delivery') != 'gmail' or
            runtime.get('local_staff_auth') is not False or runtime.get('staff_password_auth') is not True):
        raise ValueError('public_service_runtime_mismatch')
    html = local_http(WEB_PORT, '/report?loc=GWANAK_60', host=host)
    asset = re.search(rb'src="(/assets/[a-zA-Z0-9_-]+\.js)"', html)
    if b'<div id="root">' not in html or not asset or not local_http(WEB_PORT, asset[1].decode('ascii'), host=host):
        raise ValueError('public_service_bundle_missing')


def worker_heartbeat_expiries(redis, keys):
    now = time.monotonic()
    ttl = redis.pipeline(transaction=False)
    for key in keys:
        ttl.pttl(key)
    return [now + ms / 1000 if ms > 0 else 0 for ms in ttl.execute()]


def stop_children(children):
    active = [child for child in reversed(children.values()) if child.poll() is None]
    for child in active:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10
    for child in active:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=3)


def run_public():
    from booth_local import BoothError, service_binary, system_lsof

    require_open_ports()
    sys.path.insert(0, str(ROOT / 'apps/api'))
    origin = read_public_origin()
    env = environment('dev', public_origin=origin)
    try:
        node = service_binary('node')
        lsof = system_lsof()
    except BoothError:
        raise PublicServiceError('Public service startup requires Node.js and lsof.') from None
    children = {}
    shutdown_requested = False

    def shutdown(_signum, _frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    def ensure_running(*, completed=None):
        if shutdown_requested:
            raise KeyboardInterrupt
        for name, child in children.items():
            if name != completed and child.poll() is not None:
                raise PublicServiceError(f'Public service component exited: {name}.')

    def launch(name, command, *, cwd=ROOT / 'apps/api', child_env=env):
        ensure_running()
        children[name] = subprocess.Popen(command, cwd=cwd, env=child_env, start_new_session=True,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)

    def await_ready(check, description, *, timeout=20, completed=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ensure_running(completed=completed)
            if check():
                return
            time.sleep(0.15)
        raise PublicServiceError(f'Public service readiness timed out: {description}.')

    def fresh_listener(name, port):
        child = children[name]
        result = subprocess.run([lsof, '-nP', '-a', '-p', str(child.pid), '-iTCP@127.0.0.1:' + str(port),
                                 '-sTCP:LISTEN', '-t'], capture_output=True, timeout=3, text=True)
        return result.returncode == 0 and result.stdout.strip() == str(child.pid)

    def ready_web():
        try:
            verify_public_web(urlsplit(origin).netloc)
            return True
        except (OSError, http.client.HTTPException, ValueError):
            return False

    lock_fd = os.open(BASE / 'public-run.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    previous_handlers = {}
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            raise PublicServiceError('The public service lock must be a private, owner-only file.')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicServiceError('Another public service launcher is already running.') from None
        previous_handlers = {sig: signal.signal(sig, shutdown) for sig in (signal.SIGINT, signal.SIGTERM)}
        with tempfile.TemporaryDirectory(prefix='public-web-', dir=BASE) as directory:
            cache = Path(directory).resolve()
            web = ROOT / 'apps/web'
            web_env = {
                'PATH': '/usr/bin:/bin', 'HOME': str(cache), 'LC_ALL': 'C', 'NODE_ENV': 'production',
                'BARO_WEB_CACHE': str(cache), 'BARO_WEB_MODE': 'service', 'BARO_PUBLIC_ORIGIN': origin,
                'BARO_WEB_PORT': str(WEB_PORT), 'BARO_API_PORT': str(API_PORT),
            }
            web_command = [node, str(web / 'scripts/booth-server.mjs')]
            try:
                print('Building service web assets.', flush=True)
                launch('web build', [*web_command, '--build'], cwd=web, child_env=web_env)
                build = children['web build']
                await_ready(lambda: build.poll() is not None, 'web build', timeout=60, completed='web build')
                del children['web build']
                if build.returncode != 0:
                    raise PublicServiceError('Service web build failed. Check the project web dependencies.')
                ensure_running()
                require_open_ports()
                dev.start()  # Existing project-owned PostgreSQL.
                redis_server = redis_runtime()
                redis_server.start()  # Authenticates and verifies the existing owned instance.
                from app.jobs import queue_name as analysis_queue
                from app.mail_jobs import queue_name as mail_queue
                from arq.constants import health_check_key_suffix
                from redis import Redis

                scope = SimpleNamespace(app_env='dev', deployment_mode='service')
                queues = [f'{analysis_queue(scope)}:health', mail_queue(scope) + health_check_key_suffix]
                launch('api', api_command())
                await_ready(lambda: fresh_listener('api', API_PORT), 'API listener')
                launch('web', web_command, cwd=web, child_env=web_env)
                await_ready(lambda: fresh_listener('web', WEB_PORT), 'web listener')
                await_ready(ready_web, 'web bundle, PostgreSQL and service runtime')
                with Redis.from_url(env['REDIS_URL'], socket_timeout=2, socket_connect_timeout=2) as redis:
                    before = worker_heartbeat_expiries(redis, queues)
                    launch('analysis worker', [sys.executable, '-m', 'app.jobs', 'worker'])
                    launch('mail worker', [sys.executable, '-m', 'app.mail_jobs', 'worker'])
                    await_ready(lambda: all(current > prior + 0.1 for current, prior in zip(
                        worker_heartbeat_expiries(redis, queues), before, strict=True)), 'Redis worker heartbeats')
                launch('analysis dispatch', [sys.executable, '-m', 'app.jobs', 'dispatch'])
                launch('mail dispatch', [sys.executable, '-m', 'app.mail_jobs', 'dispatch'])
                ensure_running()
                print(PUBLIC_READY, flush=True)
                while True:
                    ensure_running()
                    time.sleep(0.25)
            finally:
                stop_children(children)
    finally:
        os.close(lock_fd)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['configure', 'export-configure', 'mail-configure', 'mail-worker', 'mail-dispatch', 'redis-start', 'redis-status', 'migrate', 'api', 'run-public', 'worker', 'dispatch', 'staff-role', 'rename-staff'])
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--user-id')
    parser.add_argument('--expected-role', choices=['reporter', 'dispatcher', 'worker'])
    parser.add_argument('--target-role', choices=['reporter', 'dispatcher', 'worker'])
    parser.add_argument('--reason')
    parser.add_argument('--display-name')
    parser.add_argument('--expected-display-name')
    args = parser.parse_args()
    if args.command == 'configure':
        configure()
        return 0
    if args.command == 'mail-configure':
        configure_mail()
        return 0
    if args.command == 'export-configure':
        configure_exports()
        return 0
    if args.command.startswith('redis-'):
        runtime = redis_runtime()
        if args.command == 'redis-start':
            runtime.start()
        else:
            runtime.status()
        return 0
    if args.test and args.command != 'migrate':
        raise RuntimeError('Test scope is only available for migrations')
    if args.command == 'run-public':
        return run_public()
    env = environment('test' if args.test else 'dev')
    if args.command == 'migrate':
        command = [sys.executable, '-m', 'alembic', 'upgrade', 'head']
    elif args.command == 'api':
        command = api_command()
    elif args.command in {'staff-role', 'rename-staff'}:
        if not all((args.user_id, args.expected_role, args.reason)) or (args.command == 'staff-role' and not args.target_role) or (args.command == 'rename-staff' and (args.expected_display_name is None or args.display_name is None)):
            raise RuntimeError('Explicit verified account, expected role, target role and reason required')
        command = [sys.executable, '-m', 'app.service_admin', '--user-id', args.user_id,
                   '--expected-role', args.expected_role, '--reason', args.reason]
        if args.command == 'staff-role':
            command += ['--target-role', args.target_role]
        else:
            command += ['--rename-staff', '--expected-display-name', args.expected_display_name]
        if args.display_name is not None:
            command += ['--display-name', args.display_name]
    elif args.command.startswith('mail-'):
        command = [sys.executable, '-m', 'app.mail_jobs', args.command.removeprefix('mail-')]
    else:
        command = [sys.executable, '-m', 'app.jobs', args.command]
    if args.command == 'migrate':
        result = subprocess.run(command, cwd=ROOT / 'apps/api', env=env, capture_output=True)
        if result.returncode:
            raise RuntimeError('Service migration failed; sensitive diagnostics withheld')
        print('Isolated service schema upgraded.')
        return 0
    return subprocess.call(command, cwd=ROOT / 'apps/api', env=env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except PublicServiceError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        print(f'Service operation refused ({type(exc).__name__}); private details withheld.', file=sys.stderr)
        raise SystemExit(1) from None

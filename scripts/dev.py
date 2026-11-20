#!/usr/bin/env python3
"""Project-local PostgreSQL lifecycle. Never uses the Homebrew default cluster."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
DATA = LOCAL / 'postgres'
SETTINGS = LOCAL / 'postgres-settings.json'
PORT = '55432'
ADMIN = 'baro_local_admin'


def binaries():
    candidates = [os.environ.get('BARO_PG_BIN', ''), '/opt/homebrew/opt/postgresql@16/bin', '/usr/local/opt/postgresql@16/bin']
    for candidate in candidates:
        if candidate and (Path(candidate) / 'initdb').is_file():
            return Path(candidate)
    raise RuntimeError('PostgreSQL 16 도구가 없습니다. 설치 후 BARO_PG_BIN에 bin 경로를 지정하세요.')


def run(name, *args, env=None, capture=False):
    result = subprocess.run([str(binaries() / name), *map(str, args)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # PostgreSQL errors can contain SQL or credentials; do not echo raw output.
        raise RuntimeError(f'{name} 실행 실패 (종료 코드 {result.returncode}). 로컬 상태와 포트 충돌을 확인하세요.')
    if not capture and result.stdout and name in {'initdb', 'pg_ctl'}:
        print(f'{name}: 성공')
    return result.stdout


def settings(create=False):
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    if not SETTINGS.exists():
        if not create:
            raise RuntimeError('먼저 db-init을 실행하세요.')
        value = {'admin_password': secrets.token_urlsafe(32), 'password': secrets.token_urlsafe(32)}
        fd = os.open(SETTINGS, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
    if SETTINGS.is_symlink() or SETTINGS.stat().st_mode & 0o077:
        raise RuntimeError('로컬 DB 설정 파일은 일반 파일이며 권한 600이어야 합니다.')
    return json.loads(SETTINGS.read_text())


def owned():
    if DATA.is_symlink() or not (DATA / 'PG_VERSION').exists() or not (LOCAL / 'postgres-owner').exists():
        raise RuntimeError('이 도구가 초기화한 프로젝트 DB가 아닙니다. 자동 수정하지 않습니다.')
    if (LOCAL / 'postgres-owner').read_text().strip() != str(ROOT):
        raise RuntimeError('DB 소유 경로가 현재 프로젝트와 다릅니다.')


def running():
    pidfile = DATA / 'postmaster.pid'
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().splitlines()[0])
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        raise RuntimeError('DB 프로세스 확인 권한이 없습니다. 샌드박스 밖의 승인된 실행으로 다시 확인하세요.')
    return True


def sql(statement, database='postgres'):
    value = settings()
    env = {**os.environ, 'PGPASSWORD': value['admin_password']}
    # SQL goes through stdin, never process arguments or tool output.
    result = subprocess.run([str(binaries() / 'psql'), '-X', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-p', PORT, '-U', ADMIN, '-d', database], input=statement, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('프로젝트 DB 초기 SQL 실행 실패. 원문은 비밀 보호를 위해 출력하지 않습니다.')
    return result.stdout


def start():
    owned()
    if not running():
        run('pg_ctl', '-D', DATA, '-l', LOCAL / 'postgres.log', '-w', 'start')
    print(f'프로젝트 PostgreSQL 실행 중: 127.0.0.1:{PORT}')


def init():
    value = settings(create=True)
    if DATA.exists():
        owned()
        print('기존 프로젝트 DB 보존')
    else:
        with tempfile.NamedTemporaryFile(mode='w', dir=LOCAL, prefix='pg-password-') as handle:
            os.chmod(handle.name, 0o600)
            handle.write(value['admin_password'] + '\n')
            handle.flush()
            run('initdb', '-D', DATA, '-U', ADMIN, '--pwfile', handle.name, '--auth-local=scram-sha-256', '--auth-host=scram-sha-256', '--encoding=UTF8', '--locale=C')
        with (DATA / 'postgresql.conf').open('a') as handle:
            handle.write("\nlisten_addresses = '127.0.0.1'\nport = 55432\nunix_socket_directories = ''\npassword_encryption = 'scram-sha-256'\n")
        (LOCAL / 'postgres-owner').write_text(str(ROOT) + '\n')
    start()
    # Generated passwords contain URL-safe characters, no SQL quote characters.
    sql("SELECT 'CREATE ROLE baro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD ''" + value['password'] + "''' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='baro')\\gexec\n")
    for database in ('baro_dev', 'baro_test'):
        sql(f"SELECT 'CREATE DATABASE {database} OWNER baro' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='{database}')\\gexec\n")
        sql('REVOKE CREATE ON SCHEMA public FROM PUBLIC;', database)
    print('개발 DB와 별도 테스트 DB 준비 완료')


def configure():
    value = settings()
    target = ROOT / '.env'
    if target.exists() or target.is_symlink():
        print('기존 .env 보존: 자동 덮어쓰기 없음')
        return
    template = (ROOT / '.env.example').read_text()
    replacements = {'JWT_SECRET': secrets.token_urlsafe(48), 'DATABASE_URL': f"postgresql+asyncpg://baro:{value['password']}@127.0.0.1:{PORT}/baro_dev", 'TEST_DATABASE_URL': f"postgresql+asyncpg://baro:{value['password']}@127.0.0.1:{PORT}/baro_test", 'DEMO_AUTH': 'true', 'APP_ENV': 'dev', 'LLM_FAKE': 'true', 'AUTO_CONFIRM': 'false', 'PILOT_NOTICE': 'true', 'APP_BASE_URL': 'http://127.0.0.1:5173'}
    lines = []
    replacements['EXPORT_ENCRYPTION_KEY'] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii')
    for line in template.splitlines():
        key = line.split('=', 1)[0]
        lines.append(f'{key}={replacements.pop(key)}' if key in replacements else line)
    lines.extend(f'{key}={value}' for key, value in replacements.items())
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(lines) + '\n')
    print('.env 생성 완료 (권한 600, 비밀값 출력 없음)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['db-init', 'db-start', 'db-stop', 'db-status', 'configure'])
    command = parser.parse_args().command
    try:
        if command == 'db-init':
            init()
        elif command == 'configure':
            configure()
        else:
            owned()
            if command == 'db-start':
                start()
            elif command == 'db-stop':
                if running():
                    run('pg_ctl', '-D', DATA, '-m', 'fast', '-w', 'stop')
                print('프로젝트 PostgreSQL 중지 상태')
            else:
                print(f'프로젝트 PostgreSQL: {"실행 중" if running() else "중지"}; 데이터: {DATA}')
    except (RuntimeError, OSError, ValueError) as exc:
        print(f'오류: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

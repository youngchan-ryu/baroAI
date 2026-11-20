"""Local operator CLI for existing verified service accounts; never creates accounts."""

import argparse
import asyncio
import os
import sys
import unicodedata
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.auth_service import ensure_namespace, service_account_eligible
from app.config import Settings
from app.db import Database
from app.models import AuthRoleChange, AuthSession, StaffNameChange, User


def valid_name(value):
    if not isinstance(value, str) or any(
        unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in value
    ):
        raise ValueError("invalid_staff_name")
    value = value.strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("invalid_staff_name")
    return value


def audit_name(db, user, name, reason):
    if user.display_name != name:
        db.add(
            StaffNameChange(
                user_id=user.id,
                before_name=user.display_name,
                after_name=name,
                reason=reason.strip(),
                operator_uid=os.getuid(),
            )
        )
        user.display_name = name


async def change_role(
    database, settings, *, user_id, expected_role, target_role, reason, display_name=None
):
    if settings.deployment_mode != "service":
        raise ValueError("service_mode_required")
    if (
        target_role not in {"reporter", "dispatcher", "worker"}
        or expected_role not in {"reporter", "dispatcher", "worker"}
        or not reason.strip()
        or len(reason) > 1000
    ):
        raise ValueError("invalid_role_change")
    name = valid_name(display_name) if target_role in {"worker", "dispatcher"} else None
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        await ensure_namespace(db, settings)
        # NO KEY UPDATE serializes OTP/login/role changes while permitting FK key-share
        # reads by active requests that already hold an AuthSession row lock.
        user = await db.scalar(
            select(User).where(User.id == user_id).with_for_update(key_share=True)
        )
        if not user or user.persona is not None:
            raise ValueError("verified_service_account_required")
        # A former staff account remains removable even if its mailbox no
        # longer satisfies policy. Never grant staff capabilities to it.
        if not service_account_eligible(user) and target_role != "reporter":
            raise ValueError("service_email_domain_required")
        if user.role != expected_role or user.role == target_role:
            raise ValueError("role_version_conflict")
        if name is not None:
            audit_name(db, user, name, reason)
        user.role = target_role
        user.auth_epoch += 1
        # Keep a deterministic order when revoking multiple sessions.
        sessions = list(
            (
                await db.scalars(
                    select(AuthSession)
                    .where(AuthSession.user_id == user.id)
                    .order_by(AuthSession.id)
                    .with_for_update()
                )
            ).all()
        )
        for session in sessions:
            session.revoked = True
        db.add(
            AuthRoleChange(
                user_id=user.id,
                from_role=expected_role,
                to_role=target_role,
                reason=reason.strip(),
                operator_uid=os.getuid(),
            )
        )
        await db.flush()


async def rename_staff(
    database, settings, *, user_id, expected_role, expected_display_name, display_name, reason
):
    if settings.deployment_mode != "service":
        raise ValueError("service_mode_required")
    if expected_role not in {"worker", "dispatcher"} or not reason.strip() or len(reason) > 1000:
        raise ValueError("invalid_name_change")
    name = valid_name(display_name)
    async with database.transaction() as db:
        from app.dispatch_lock import dispatch_lock

        await dispatch_lock(db)
        await ensure_namespace(db, settings)
        user = await db.scalar(
            select(User).where(User.id == user_id).with_for_update(key_share=True)
        )
        if (
            not user
            or user.persona is not None
            or user.role != expected_role
            or user.display_name != expected_display_name
        ):
            raise ValueError("name_version_conflict")
        changed = user.display_name != name
        audit_name(db, user, name, reason)
        await db.flush()
        return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rename-staff", action="store_true")
    parser.add_argument("--display-name")
    parser.add_argument("--expected-display-name")
    parser.add_argument("--user-id", type=uuid.UUID, required=True)
    parser.add_argument(
        "--expected-role", choices=["reporter", "dispatcher", "worker"], required=True
    )
    parser.add_argument("--target-role", choices=["reporter", "dispatcher", "worker"])
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    async def run():
        settings = Settings()
        database = Database(settings)
        try:
            if args.rename_staff:
                return await rename_staff(
                    database,
                    settings,
                    user_id=args.user_id,
                    expected_role=args.expected_role,
                    expected_display_name=args.expected_display_name,
                    display_name=args.display_name,
                    reason=args.reason,
                )
            else:
                await change_role(
                    database,
                    settings,
                    user_id=args.user_id,
                    expected_role=args.expected_role,
                    target_role=args.target_role,
                    reason=args.reason,
                    display_name=args.display_name,
                )
        finally:
            await database.close()

    try:
        changed = asyncio.run(run())
    except (ValueError, HTTPException, OSError):
        print(
            "Service role change refused; no account created or secret details printed.",
            file=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 - database exceptions can expose credentials
        print("Service role change failed safely; details redacted.", file=sys.stderr)
        return 1
    print(
        ("Staff name updated and audited." if changed else "Staff name unchanged; no audit added.")
        if args.rename_staff
        else "Service role updated, audited, and previous sessions/tokens revoked."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

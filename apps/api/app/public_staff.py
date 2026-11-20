"""Public staff invitations. The operator is the holder of the dedicated DB credential."""

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import timedelta

from sqlalchemy import select, text

from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.models import AuthSession, DeploymentIdentity, PublicStaffInvite, User
from app.security import now, token_digest
from app.service_admin import valid_name


def invite_digest(settings, token):
    return token_digest(settings, "staff-invite:" + token)


async def require_existing_marker(db, settings):
    if settings.deployment_mode != "public_demo":
        raise ValueError("public_mode_required")
    if await db.scalar(text("SELECT current_schema()")) != "public":
        raise ValueError("public_schema_mismatch")
    marker = await db.get(DeploymentIdentity, 1)
    if (
        not marker
        or marker.mode != "public_demo"
        or marker.namespace != settings.public_demo_namespace
    ):
        raise ValueError("public_marker_required")


async def staff_session_active(db, user_id):
    return bool(
        await db.scalar(
            select(PublicStaffInvite.id)
            .join(AuthSession, AuthSession.id == PublicStaffInvite.session_id)
            .where(
                PublicStaffInvite.user_id == user_id,
                PublicStaffInvite.role == "worker",
                PublicStaffInvite.revoked.is_(False),
                PublicStaffInvite.consumed_at.is_not(None),
                AuthSession.revoked.is_(False),
                AuthSession.expires_at > now(),
            )
            .limit(1)
        )
    )


async def issue_invite(database, settings, *, role, display_name=None, user_id=None):
    if role not in {"dispatcher", "worker"} or bool(display_name) == bool(user_id):
        raise ValueError("invalid_staff_invite")
    name = valid_name(display_name) if display_name else None
    token = secrets.token_urlsafe(32)
    async with database.transaction() as db:
        await dispatch_lock(db)
        await require_existing_marker(db, settings)
        if user_id:
            user = await db.get(User, user_id, with_for_update=True)
            if not user or user.role != role or user.persona is not None:
                raise ValueError("invalid_staff_identity")
        else:
            user = User(role=role, display_name=name)
            db.add(user)
            await db.flush()
        invite = PublicStaffInvite(
            user_id=user.id,
            role=role,
            token_hash=invite_digest(settings, token),
            expires_at=now() + timedelta(minutes=30),
            revoked=False,
        )
        db.add(invite)
        await db.flush()
        return invite.id, user.id, token


async def revoke_invite(database, settings, invite_id):
    async with database.transaction() as db:
        await dispatch_lock(db)
        await require_existing_marker(db, settings)
        invite = await db.get(PublicStaffInvite, invite_id, with_for_update=True)
        if not invite:
            raise ValueError("invite_not_found")
        invite.revoked = True


def main():
    parser = argparse.ArgumentParser(description="Public demo staff invitation operator")
    commands = parser.add_subparsers(dest="action", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--role", choices=["dispatcher", "worker"], required=True)
    identity = issue.add_mutually_exclusive_group(required=True)
    identity.add_argument("--display-name")
    identity.add_argument("--user-id", type=uuid.UUID)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("invite_id", type=uuid.UUID)
    args = parser.parse_args()
    if args.action == "issue" and not sys.stdout.isatty():
        parser.error("issue requires a private interactive terminal")

    async def run():
        settings = Settings()
        database = Database(settings)
        try:
            if args.action == "issue":
                invite_id, user_id, token = await issue_invite(
                    database,
                    settings,
                    role=args.role,
                    display_name=args.display_name,
                    user_id=args.user_id,
                )
                print(f"Invite ID: {invite_id}\nStaff ID: {user_id}")
                print(f"{settings.app_base_url}/staff-invite#token={token}")
            else:
                await revoke_invite(database, settings, args.invite_id)
                print("Invite revoked.")
        finally:
            await database.close()

    try:
        asyncio.run(run())
    except Exception:
        print("Public staff operation failed.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

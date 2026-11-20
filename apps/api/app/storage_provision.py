"""Explicit two-stage provisioning for empty, isolated stores; no photo cleanup."""

import json
import os
import stat
import uuid

from sqlalchemy import exists, select, text

from app.models import DeploymentIdentity, ImageVariant, StorageIdentity, Upload
from app.storage import MARKER, async_locked_directory


async def storage_lock(db):
    # Must precede the directory flock and all deployment/object row locks.
    await db.execute(text("SELECT pg_advisory_xact_lock(24002219)"))


def marker_bytes(identity):
    return json.dumps(
        {
            "version": 1,
            "instance_id": str(identity.instance_id),
            "storage_id": str(identity.storage_id),
            "operation_id": str(identity.operation_id),
            "mode": identity.mode,
            "environment": identity.environment,
            "namespace": identity.namespace,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def check_scope(identity, settings, operation_id, directory):
    st = os.fstat(directory)
    from app.auth_service import namespace_for

    namespace = namespace_for(settings)
    if (
        identity.operation_id != operation_id
        or (identity.mode, identity.environment, identity.namespace)
        != (settings.deployment_mode, settings.app_env, namespace)
        or (identity.device, identity.inode) != (st.st_dev, st.st_ino)
    ):
        raise ValueError("storage provisioning identity mismatch")


async def check_empty_database(db, settings):
    marker = await db.scalar(select(DeploymentIdentity).where(DeploymentIdentity.id == 1))
    from app.auth_service import namespace_for

    namespace = namespace_for(settings)
    if marker is None or (marker.mode, marker.namespace) != (settings.deployment_mode, namespace):
        raise ValueError("deployment identity mismatch")
    if await db.scalar(select(exists().where(Upload.id.is_not(None)))) or await db.scalar(
        select(exists().where(ImageVariant.id.is_not(None)))
    ):
        raise ValueError("existing photos cannot be attached")


async def prepare(database, settings, operation_id):
    """Persist a pending intent. Retrying requires the same explicit operation ID."""
    from app.auth_service import namespace_for
    if settings.app_env not in {"dev", "test"} or not isinstance(operation_id, uuid.UUID):
        raise ValueError("local environment and operation UUID required")
    async with database.transaction() as db:
        await storage_lock(db)
        async with async_locked_directory(settings.storage_path) as directory:
            identity = await db.get(StorageIdentity, 1, with_for_update=True)
            await check_empty_database(db, settings)
            if identity:
                check_scope(identity, settings, operation_id, directory)
                if identity.state != "pending":
                    raise ValueError("storage already active")
            else:
                if os.listdir(directory):
                    raise ValueError("storage directory is not empty")
                st = os.fstat(directory)
                identity = StorageIdentity(
                    id=1,
                    instance_id=uuid.uuid4(),
                    storage_id=uuid.uuid4(),
                    operation_id=operation_id,
                    mode=settings.deployment_mode,
                    environment=settings.app_env,
                    namespace=namespace_for(settings),
                    state="pending",
                    device=st.st_dev,
                    inode=st.st_ino,
                )
                db.add(identity)
            return identity.storage_id


async def activate(database, settings, operation_id):
    """Complete only the caller's pending intent, allowing the same marker on retry."""
    if settings.app_env not in {"dev", "test"} or not isinstance(operation_id, uuid.UUID):
        raise ValueError("local environment and operation UUID required")
    async with database.transaction() as db:
        await storage_lock(db)
        async with async_locked_directory(settings.storage_path) as directory:
            identity = await db.get(StorageIdentity, 1, with_for_update=True)
            if identity is None:
                raise ValueError("storage provisioning intent missing")
            check_scope(identity, settings, operation_id, directory)
            await check_empty_database(db, settings)
            if identity.state != "pending":
                raise ValueError("storage is not pending")
            stage = f".baroai-init-{operation_id.hex}"
            names = set(os.listdir(directory))
            if names - {MARKER, stage}:
                raise ValueError("storage directory is not empty")
            expected = marker_bytes(identity)
            if MARKER not in names:
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
                staged = os.open(stage, flags, 0o600, dir_fd=directory)
                with os.fdopen(staged, "r+b") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("invalid staging file")
                    current = stream.read(4097)
                    if not expected.startswith(current):
                        raise ValueError("storage staging mismatch")
                    stream.seek(0)
                    stream.write(expected)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(
                    stage, MARKER, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
                )
                os.fsync(directory)
            source = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(source, "rb") as stream:
                if (
                    not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                    or stream.read(4097) != expected
                ):
                    raise ValueError("storage marker mismatch")
            if stage in os.listdir(directory):
                staged = os.open(
                    stage, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                with os.fdopen(staged, "rb") as stream:
                    if (
                        not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                        or stream.read(4097) != expected
                    ):
                        raise ValueError("storage staging mismatch")
                os.unlink(stage, dir_fd=directory)
                os.fsync(directory)
            identity.state = "active"
            return identity.storage_id

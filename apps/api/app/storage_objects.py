"""Unpublished managed photo intents. No API or cleanup path is enabled yet."""

import hashlib
import os
import stat
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.dispatch_lock import dispatch_lock
from app.models import StorageIdentity, StorageObject
from app.security import now
from app.storage import MARKER, LocalPrivateStorage, async_locked_directory, verify_directory
from app.storage_provision import check_scope, marker_bytes, storage_lock


def verify_marker(directory, identity):
    source = os.open(MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(source, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.read(4097) != marker_bytes(
            identity
        ):
            raise ValueError("storage marker mismatch")


async def checked_identity(db, settings, directory):
    identity = await db.get(StorageIdentity, 1, with_for_update=True)
    if identity is None or identity.state != "active":
        raise ValueError("active storage identity required")
    check_scope(identity, settings, identity.operation_id, directory)
    verify_marker(directory, identity)
    return identity


@asynccontextmanager
async def managed_transaction(database, settings):
    """Acquire storage gates before any authentication or business-row locks."""
    async with database.transaction() as db:
        await dispatch_lock(db)
        await storage_lock(db)
        async with async_locked_directory(settings.storage_path) as directory:
            identity = await checked_identity(db, settings, directory)
            db.info["managed_photo_access"] = (identity, directory)
            try:
                yield db, identity, directory
            finally:
                db.info.pop("managed_photo_access", None)


async def reserve_locked(db, identity, *, owner_id, kind, content, delete_after, operation_id=None):
    """Caller authenticates inside managed_transaction and commits before writing."""
    stamp = now()
    if (
        kind not in {"original", "variant"}
        or not content
        or not stamp < delete_after <= stamp + timedelta(days=30)
    ):
        raise ValueError("invalid object intent")
    obj = StorageObject(
        id=uuid.uuid4(),
        storage_id=identity.storage_id,
        owner_id=owner_id,
        operation_id=operation_id,
        object_key=uuid.uuid4().hex + ".jpg",
        kind=kind,
        state="pending",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        delete_after=delete_after,
    )
    db.add(obj)
    await db.flush()
    obj.delete_after = min(obj.delete_after, obj.created_at + timedelta(days=30))
    if obj.delete_after <= now():
        raise ValueError("object intent expired")
    return obj.id


async def pending_locked(db, identity, settings, object_id, content, *, directory, request=None):
    candidate = await db.get(StorageObject, object_id)
    if candidate and candidate.operation_id:
        if request is None:
            raise ValueError("operation authorization required")
        from app.storage_operations import checked_operation

        operation = await checked_operation(
            db, identity, directory, request, candidate.operation_id
        )
        if operation.state != "active":
            from fastapi import HTTPException

            raise HTTPException(409, "operation_not_active")
        if (
            candidate.owner_id != operation.actor_id
            or not operation.result_data
            or str(object_id) not in operation.result_data.get("object_ids", [])
        ):
            raise ValueError("operation object mismatch")
    elif request is not None:
        raise ValueError("unlinked object")
    obj = await db.scalar(
        select(StorageObject).where(StorageObject.id == object_id).with_for_update()
    )
    if (
        obj is None
        or obj.storage_id != identity.storage_id
        or obj.state != "pending"
        or obj.delete_after <= now()
        or obj.size != len(content)
        or obj.sha256 != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("pending object mismatch")
    LocalPrivateStorage(settings.storage_path).path(obj.object_key)
    return obj


def stage_name(obj):
    return f".baroai-object-{obj.id.hex}"


def anchor_name(obj):
    return f".baroai-anchor-{obj.id.hex}"


async def prepare_staging(database, settings, object_id, content, *, request=None):
    """Commit exclusive staging inode ownership before any file contents are written."""
    async with managed_transaction(database, settings) as (db, identity, directory):
        obj = await pending_locked(
            db, identity, settings, object_id, content, directory=directory, request=request
        )
        if obj.stage_inode is not None:
            return
        try:
            os.stat(obj.object_key, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("existing pending object mismatch")
        try:
            fd = os.open(
                stage_name(obj),
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
        except FileExistsError:
            raise ValueError("unowned pending staging file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("pending staging mismatch")
            os.link(
                stage_name(obj),
                anchor_name(obj),
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            anchored = os.stat(anchor_name(obj), dir_fd=directory, follow_symlinks=False)
            if (anchored.st_dev, anchored.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("pending staging changed")
            os.fsync(fd)
            os.fsync(directory)
            verify_directory(settings.storage_path, directory)
            obj.stage_device, obj.stage_inode = info.st_dev, info.st_ino
            await db.flush()
        finally:
            os.close(fd)


async def write_pending(database, settings, object_id, content, *, request=None):
    """Write only committed intent and staging inode; no publication or deletion occurs."""
    await prepare_staging(database, settings, object_id, content, request=request)
    async with managed_transaction(database, settings) as (db, identity, directory):
        obj = await pending_locked(
            db, identity, settings, object_id, content, directory=directory, request=request
        )
        stage = stage_name(obj)
        try:
            previous = os.stat(obj.object_key, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        staged = os.open(stage, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(staged, "r+b") as stream:
            info = os.fstat(stream.fileno())
            anchor = os.stat(anchor_name(obj), dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or (anchor.st_dev, anchor.st_ino) != (info.st_dev, info.st_ino)
                or (info.st_dev, info.st_ino) != (obj.stage_device, obj.stage_inode)
                or info.st_size > obj.size
                or info.st_nlink != (3 if previous is not None else 2)
            ):
                raise ValueError("pending staging mismatch")
            if previous is not None and (previous.st_dev, previous.st_ino) != (
                info.st_dev,
                info.st_ino,
            ):
                raise ValueError("existing pending object mismatch")
            current = stream.read(obj.size + 1)
            if not content.startswith(current):
                raise ValueError("pending staging mismatch")
            stream.seek(0)
            stream.write(content)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            verify_directory(settings.storage_path, directory)
            current_info = os.stat(stage, dir_fd=directory, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise ValueError("pending staging changed")
            try:
                os.link(
                    stage,
                    obj.object_key,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                target_info = os.stat(obj.object_key, dir_fd=directory, follow_symlinks=False)
                if (info.st_dev, info.st_ino) != (target_info.st_dev, target_info.st_ino):
                    raise ValueError("existing pending object mismatch") from None
            os.fsync(directory)


async def verify_for_publication(db, identity, directory, object_id, *, owner_id, kind):
    """Lock and verify a complete pending object before atomic business publication.

    Caller must use managed_transaction, authenticate, lock business rows, validate
    owner/assignment/lineage, and set ready with its business references in this
    same transaction. No independently callable publication endpoint exists.
    """
    return await _verify_for_publication(
        db, identity, directory, object_id, owner_id=owner_id, kind=kind, operation_id=None
    )


async def _verify_for_publication(
    db, identity, directory, object_id, *, owner_id, kind, operation_id
):
    obj = await db.scalar(
        select(StorageObject).where(StorageObject.id == object_id).with_for_update()
    )
    if (
        obj is None
        or obj.operation_id != operation_id
        or obj.storage_id != identity.storage_id
        or obj.owner_id != owner_id
        or obj.kind != kind
        or obj.state != "pending"
        or obj.delete_after <= now()
        or obj.stage_device is None
        or obj.stage_inode is None
        or obj.delete_after > obj.created_at + timedelta(days=30)
    ):
        raise ValueError("pending publication mismatch")
    LocalPrivateStorage(Path("/")).path(obj.object_key)
    stage = os.stat(stage_name(obj), dir_fd=directory, follow_symlinks=False)
    anchor = os.stat(anchor_name(obj), dir_fd=directory, follow_symlinks=False)
    target = os.open(obj.object_key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(target, "rb") as stream:
        info = os.fstat(stream.fileno())
        expected = (obj.stage_device, obj.stage_inode)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 3
            or info.st_size != obj.size
            or (info.st_dev, info.st_ino) != expected
            or (stage.st_dev, stage.st_ino) != expected
            or (anchor.st_dev, anchor.st_ino) != expected
            or hashlib.sha256(stream.read(obj.size + 1)).hexdigest() != obj.sha256
        ):
            raise ValueError("pending publication file mismatch")
    return obj


async def read_ready(db, identity, directory, *, key, owner_id, kind):
    """Read a ready managed object only while the caller holds its business locks."""
    LocalPrivateStorage(Path("/")).path(key)
    obj = await db.scalar(
        select(StorageObject).where(StorageObject.object_key == key).with_for_update()
    )
    if (
        obj is None
        or obj.storage_id != identity.storage_id
        or obj.owner_id != owner_id
        or obj.kind != kind
        or obj.state != "ready"
        or obj.delete_after <= now()
        or obj.stage_device is None
        or obj.stage_inode is None
        or obj.delete_after > obj.created_at + timedelta(days=30)
    ):
        raise ValueError("ready object mismatch")
    expected = (obj.stage_device, obj.stage_inode)
    stage = os.stat(stage_name(obj), dir_fd=directory, follow_symlinks=False)
    anchor = os.stat(anchor_name(obj), dir_fd=directory, follow_symlinks=False)
    source = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 3
            or info.st_size != obj.size
            or (info.st_dev, info.st_ino) != expected
            or (stage.st_dev, stage.st_ino) != expected
            or (anchor.st_dev, anchor.st_ino) != expected
        ):
            raise ValueError("ready object file mismatch")
        data = stream.read(obj.size + 1)
        if hashlib.sha256(data).hexdigest() != obj.sha256:
            raise ValueError("ready object file mismatch")
    return data

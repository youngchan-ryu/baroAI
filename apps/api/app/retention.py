"""Explicit local retention command; dry-run by default."""

import argparse
import asyncio
import json
import re
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import exists, or_, select
from sqlalchemy.engine import make_url
from starlette.concurrency import run_in_threadpool

from app.config import ROOT, Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.export_routes import namespace
from app.models import DeploymentIdentity, ExportRecord, ImageVariant, Upload
from app.security import now
from app.storage import LocalPrivateStorage


async def purge_expired(db, storage, *, dry_run=True, at=None, upload_ids=None):
    if not dry_run:
        raise ValueError("photo cleanup requires verified storage ownership")
    at = at or now()
    expired_variant = exists().where(
        ImageVariant.upload_id == Upload.id,
        ImageVariant.object_key.is_not(None),
        ImageVariant.delete_after <= at,
    )
    query = select(Upload).where(or_(Upload.delete_after <= at, Upload.deleted, expired_variant))
    if upload_ids is not None:
        query = query.where(Upload.id.in_(upload_ids))
    uploads = (await db.scalars(query.order_by(Upload.id).with_for_update())).all()
    result = {
        "dry_run": True,
        "expired_uploads": 0,
        "deleted_objects": 0,
        "failed_uploads": 0,
        "failed_objects": 0,
        "remaining_objects": 0,
    }
    for upload in uploads:
        variants = (
            await db.scalars(select(ImageVariant).where(ImageVariant.upload_id == upload.id))
        ).all()
        expired_parent = upload.deleted or upload.delete_after <= at
        if expired_parent and not upload.deleted:
            result["expired_uploads"] += 1
        keys = {
            v.object_key
            for v in variants
            if v.object_key and (expired_parent or v.delete_after <= at)
        }
        if expired_parent:
            keys.add(upload.object_key)
        for key in keys:
            try:
                await run_in_threadpool(storage.path(key).lstat)
                result["deleted_objects"] += 1
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                result["failed_objects"] += 1
                result["remaining_objects"] += 1
    if upload_ids is None:
        known = set((await db.scalars(select(Upload.object_key))).all())
        known.update(
            (
                await db.scalars(
                    select(ImageVariant.object_key).where(ImageVariant.object_key.is_not(None))
                )
            ).all()
        )
        orphan_failures = []
        result["orphan_objects"] = await run_in_threadpool(
            purge_orphans, storage, known, at, dry_run, orphan_failures
        )
        result["failed_objects"] += len(orphan_failures)
        result["remaining_objects"] += len(orphan_failures)
    return result


def purge_orphans(storage, known, at, dry_run=True, failures=None):
    """Only UUID JPEGs in this dedicated managed directory, unreferenced for 30+ days."""
    if not dry_run:
        raise ValueError("orphan cleanup requires verified storage ownership")
    count = 0
    if not storage.root.exists():
        return count
    cutoff = (at - timedelta(days=30)).timestamp()
    for path in storage.root.iterdir():
        if (
            path.name in known
            or not re.fullmatch(r"[a-f0-9]{32}\.jpg", path.name)
            or path.is_symlink()
        ):
            continue
        try:
            if not path.is_file() or path.stat().st_mtime > cutoff:
                continue
            count += 1
        except OSError:
            if failures is not None:
                failures.append(path.name)
            continue
    return count


async def run_retention(database, settings, storage, *, dry_run=True, at=None):
    """Validate an existing deployment marker before any database or file mutation."""
    async with database.transaction() as db:
        await dispatch_lock(db)
        marker = await db.scalar(
            select(DeploymentIdentity).where(DeploymentIdentity.id == 1).with_for_update()
        )
        expected = settings.service_namespace if settings.deployment_mode == "service" else "demo"
        if marker is None or (marker.mode, marker.namespace) != (
            settings.deployment_mode,
            expected,
        ):
            raise ValueError("deployment marker missing or mismatched")
        stamp = at or now()
        has_photos = await db.scalar(select(exists().where(Upload.object_key.is_not(None))))
        if not dry_run and has_photos and not storage.root.is_dir():
            raise ValueError("photo storage missing; cleanup refused")
        result = {"dry_run": dry_run, "photo_cleanup": "blocked_unverified_storage"}
        exports = (
            await db.scalars(
                select(ExportRecord)
                .where(
                    ExportRecord.namespace == namespace(settings),
                    ExportRecord.expires_at <= stamp,
                    ExportRecord.encrypted_snapshot.is_not(None),
                )
                .order_by(ExportRecord.id)
                .with_for_update()
            )
        ).all()
        result["expired_exports"] = len(exports)
        if not dry_run:
            for record in exports:
                record.encrypted_snapshot, record.expired_at = None, stamp
        return result


def validate_target(settings, mode, environment, storage_path):
    if environment not in {"dev", "test"} or (settings.deployment_mode, settings.app_env) != (
        mode,
        environment,
    ):
        raise ValueError("explicit deployment mode and local environment must match settings")
    configured = settings.storage_path.absolute()
    supplied = Path(storage_path).absolute()
    if supplied != configured or configured.resolve() != configured or len(configured.parts) < 3:
        raise ValueError("storage path must exactly match a non-symlink configured directory")
    if configured.exists() and not configured.is_dir():
        raise ValueError("storage path must be a directory")
    database = make_url(settings.database_url.get_secret_value())
    expected_database = (
        f"baro_service_{environment}" if mode == "service" else f"baro_{environment}"
    )
    if database.database != expected_database or database.host != "127.0.0.1":
        raise ValueError("retention requires the matching local database")
    if mode == "demo" and environment == "dev":
        if configured != (ROOT / "data/private/uploads").resolve():
            raise ValueError("demo storage is outside the permitted scope")
    else:
        allowed = (
            (ROOT / ".local/service" / environment).resolve()
            if mode == "service"
            else Path(tempfile.gettempdir()).resolve()
        )
        if configured == allowed or not configured.is_relative_to(allowed):
            raise ValueError("storage is outside the permitted scope")


async def run(apply, mode, environment, storage_path):
    settings = Settings()
    validate_target(settings, mode, environment, storage_path)
    database = Database(settings)
    try:
        result = await run_retention(
            database, settings, LocalPrivateStorage(settings.storage_path), dry_run=not apply
        )
        print(json.dumps(result))
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(
        description="Clean expired CSV snapshots; photo cleanup is disabled"
    )
    parser.add_argument("--mode", required=True, choices=("demo", "service"))
    parser.add_argument("--environment", required=True, choices=("dev", "test"))
    parser.add_argument(
        "--storage-path", required=True, help="Exact configured private storage directory"
    )
    parser.add_argument("--apply", action="store_true", help="Delete expired data; default dry-run")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.mode, args.environment, args.storage_path))


if __name__ == "__main__":
    main()

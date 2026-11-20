"""Provision an owned disposable demo via its official catalog and worker APIs.

This helper is invoked only by booth_local after it owns the DB and API process.
It does not accept real-service targets or enable AI calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from app.config import Settings
from app.db import Database
from app.dispatch_lock import dispatch_lock
from app.models import Assignment, DeploymentIdentity, User
from app.planning.datasets import UNVERIFIED_TRAVEL_SOURCE
from app.school_catalog_validate import validate
from app.school_catalog_xlsx import read_workbook

WORKBOOK = ROOT / "data/campus-profile/campus-profile-20261114-r08.xlsx"
WORKBOOK_SHA256 = "90e0c3c184ce566bed40b44e111f817b6d67acd53241136d0af3adb7c807bcc4"


def profile_spec(profile=None):
    if profile in (None, "r08"):
        return WORKBOOK, WORKBOOK_SHA256, "SNU_PUBLIC_20261114_R08", "2026-11-14-r08"
    if profile == "r10":
        return (ROOT / "data/campus-profile/campus-profile-20261114-r10.xlsx",
                "c5c33bdd27653c923234fd58b71107e25de3ea261e85ac90a4a3fafe9ff600dc",
                "SNU_PUBLIC_20261114_R10", "2026-11-17-r10")
    if profile == "r11":
        return (ROOT / "data/campus-profile/campus-profile-20261114-r11.xlsx",
                "f686142d8a982f4ce853d2f678b0e37d4c6b80e20225109c9551af8276a16957",
                "SNU_PUBLIC_20261114_R11", "2026-11-17-r11")
    raise PreparationError("invalid_profile")


PEOPLE = {
    "worker_a": ("합성 작업자 가", "전기 담당 1", "ELEC", "FAC_ELEC", "GWANAK_301", 510, 1050),
    "worker_b": ("합성 작업자 나", "설비 담당 1", "MECH", "FAC_MECH", "GWANAK_302", 510, 1050),
    "worker_c": (None, "건축 담당 1", "ARCH_CIVIL", "FAC_ARCH_CIVIL", "GWANAK_303", 510, 1050),
    "worker_d": (None, "토목 담당 1", "CIVIL", "FAC_CIVIL", "GWANAK_300", 510, 1050),
    "worker_e": (None, "전기 담당 2", "ELEC", "FAC_ELEC", "GWANAK_101", 420, 960),
    "worker_f": (None, "설비 담당 2", "MECH", "FAC_MECH", "GWANAK_102", 780, 1320),
    "worker_g": (None, "시설관리 담당 1", "FACILITY", "FACILITY_REVIEW", "GWANAK_103", 510, 1050),
}
SOURCE = "합성 시연 가정 · 실제 근무 일정이나 이동시간 아님"
R10_NODES = dict(zip(PEOPLE, ("GWANAK_301", "GWANAK_302", "GWANAK_60", "GWANAK_300",
                              "GWANAK_62-1", "GWANAK_301", "GWANAK_302"), strict=True))


def evidence_for_profile(profile):
    profile_spec(profile)
    if profile in ("r10", "r11"):
        return "estimated_straight_line", "© OpenStreetMap contributors, ODbL 1.0"
    return UNVERIFIED_TRAVEL_SOURCE, "2025 안내자료 등"


class PreparationError(Exception):
    pass


def validate_settings(settings):
    url = make_url(settings.database_url.get_secret_value())
    match = re.fullmatch(r"booth_[0-9a-f]{32}", url.database or "")
    if (settings.deployment_mode != "demo" or settings.app_env != "dev"
            or not settings.demo_auth or not settings.llm_fake
            or settings.planning_dataset_provider != "db" or settings.mail_delivery != "disabled"
            or os.environ.get("BARO_ISOLATED_SETTINGS") != "1"
            or os.environ.get("BARO_CAMPUS_OWNED_SUPERVISOR") != "1"
            or url.drivername != "postgresql+asyncpg" or url.host != "127.0.0.1"
            or not url.port or not match or url.username != url.database or url.query):
        raise PreparationError("owned_demo_required")


async def seed_workers(database, settings):
    validate_settings(settings)
    async with database.transaction() as db:
        await dispatch_lock(db)
        identity = await db.get(DeploymentIdentity, 1, with_for_update=True)
        if not identity or identity.mode != "demo" or identity.namespace != "demo":
            raise PreparationError("demo_identity_required")
        users = {}
        for persona in PEOPLE:
            user = await db.scalar(select(User).where(User.persona == persona).with_for_update())
            if user and (user.role != "worker" or user.verified_email is not None):
                raise PreparationError("persona_conflict")
            if persona in ("worker_a", "worker_b") and not user:
                raise PreparationError("stock_persona_missing")
            users[persona] = user
        existing = [u.id for u in users.values() if u]
        if existing and await db.scalar(select(Assignment.id).where(
            Assignment.worker_id.in_(existing), Assignment.visit_ended_at.is_(None),
            Assignment.stopped_at.is_(None), Assignment.revoked.is_(False)).limit(1)):
            raise PreparationError("active_assignment_exists")
        for persona, (stock, label, *_rest) in PEOPLE.items():
            user = users[persona]
            if user is None:
                user = User(role="worker", persona=persona, display_name=label)
                db.add(user)
                users[persona] = user
            elif stock is not None and user.display_name == stock:
                user.display_name = label
        await db.flush()
        return {persona: str(user.id) for persona, user in users.items()}


def unpack(response, status):
    if response.status_code != status:
        raise PreparationError("api_rejected")
    return response.json()


def catalog_matches(catalog, candidate, profile=None):
    source, source_version = evidence_for_profile(profile)
    return (catalog.get("revision", 0) > 0
            and all(catalog.get(k) == candidate.get(k) for k in
                    ("departments", "fields", "categories", "locations", "mappings"))
            and catalog.get("dataset") is not None
            and catalog["dataset"].get("hash") == candidate["dataset"]["sha256"]
            and catalog["dataset"].get("source") == source
            and catalog["dataset"].get("version") == profile_spec(profile)[3]
            and catalog["dataset"].get("source_version") == source_version)


def workbook_candidate(workbook, profile=None):
    _, pinned_hash, dataset_id, version = profile_spec(profile)
    source, source_version = evidence_for_profile(profile)
    if hashlib.sha256(workbook).hexdigest() != pinned_hash:
        raise PreparationError("workbook_not_pinned")
    candidate, errors, _ = validate(read_workbook(workbook))
    if errors or not candidate.get("dataset"):
        raise PreparationError("workbook_invalid")
    try:
        canonical = json.loads(candidate["dataset"]["canonical"])
        if (canonical["dataset_id"] != dataset_id
                or canonical["version"] != version
                or canonical["evidence"]["source"] != source
                or canonical["evidence"]["source_version"] != source_version):
            raise PreparationError("workbook_provenance_mismatch")
    except (KeyError, TypeError, ValueError):
        raise PreparationError("workbook_provenance_mismatch") from None
    return candidate


def inspect_workers(profiles, schedules, ids, revision, dataset, profile=None):
    profile_spec(profile)
    profiles = {str(item["worker_id"]): item for item in profiles}
    schedules = {str(item["worker_id"]): item for item in schedules}
    operations = []
    today = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    for persona, (_, _, field, department, node, shift_start, shift_end) in PEOPLE.items():
        if profile in ("r10", "r11"):
            node = R10_NODES[persona]
        worker_id = ids[persona]
        worker_profile, schedule = profiles.get(worker_id), schedules.get(worker_id)
        if worker_profile is None or schedule is None:
            raise PreparationError("worker_not_visible")
        desired_profile = {"field_codes": [field], "department_code": department}
        current_profile = {k: worker_profile.get(k) for k in desired_profile}
        if not worker_profile["revision"] and current_profile != {"field_codes": [], "department_code": None}:
            raise PreparationError("existing_profile_conflict")
        if worker_profile["revision"] and (current_profile != desired_profile
                                           or worker_profile["catalog_revision"] != revision):
            raise PreparationError("existing_profile_conflict")
        desired_schedule = {
            "enabled": True, "skills": [field], "region": "GWANAK", "shift_start_min": shift_start,
            "shift_end_min": shift_end, "start_node": node, "end_node": node,
            "days_off": [today] if persona == 'worker_e' else [],
            "assumptions_source": SOURCE, "dataset_hash": dataset["hash"],
            "dataset_revision": dataset["revision"],
        }
        if schedule["configured"]:
            current = schedule.get("data") or {}
            if any(current.get(k) != v for k, v in desired_schedule.items()):
                raise PreparationError("existing_schedule_conflict")
        elif schedule["revision"]:
            raise PreparationError("existing_schedule_conflict")
        operations.append((worker_id, worker_profile, schedule, desired_profile, desired_schedule))
    return operations


async def prepare_owned_demo(settings, api_port, profile=None):
    validate_settings(settings)
    if type(api_port) is not int or not 1024 <= api_port <= 65535:
        raise PreparationError("invalid_api_port")
    workbook = profile_spec(profile)[0].read_bytes()
    candidate = workbook_candidate(workbook) if profile is None else workbook_candidate(workbook, profile)
    database = Database(settings)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}",
                                     headers={"Origin": settings.app_base_url},
                                     trust_env=False, timeout=120) as client:
            runtime = unpack(await client.get("/v1/runtime"), 200)
            if runtime.get("mode") != "demo" or runtime.get("llm_mode") != "replay":
                raise PreparationError("api_not_demo_replay")
            session = unpack(await client.post("/v1/auth/demo", json={"persona": "dispatcher"}), 200)
            if session.get("data_scope") != "demo" or session.get("auth_mode") != "demo":
                raise PreparationError("api_session_mismatch")
            client.headers["X-CSRF-Token"] = session["csrf_token"]
            catalog = unpack(await client.get("/v1/admin/catalog"), 200)
            if catalog["revision"] and not catalog_matches(catalog, candidate, profile):
                raise PreparationError("existing_catalog_conflict")
            ids = await seed_workers(database, settings)
            profiles = unpack(await client.get("/v1/admin/workers"), 200)
            schedules = unpack(await client.get("/v1/worker-schedules"), 200)
            if not catalog["revision"]:
                if any(row.get("revision") for row in profiles if str(row["worker_id"]) in ids.values()) or any(row.get("configured") for row in schedules if str(row["worker_id"]) in ids.values()):
                    raise PreparationError("existing_worker_configuration")
                preview = unpack(await client.post("/v1/admin/catalog-imports", files={
                    "file": ("campus.xlsx", workbook, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}), 201)
                if not preview.get("valid") or preview.get("base_revision") != 0:
                    raise PreparationError("catalog_preview_invalid")
                unpack(await client.post(f"/v1/admin/catalog-imports/{preview['id']}/activate",
                    json={"expected_active_revision": 0, "reason": SOURCE},
                    headers={"Idempotency-Key": "booth-campus-20261114"}), 200)
                catalog = unpack(await client.get("/v1/admin/catalog"), 200)
                if not catalog_matches(catalog, candidate, profile):
                    raise PreparationError("catalog_verification_failed")
            revision, dataset = catalog["revision"], catalog["dataset"]
            located = unpack(await client.get("/v1/planning/location-dataset"), 200)
            if located.get("sha256") != dataset["hash"] or located.get("revision") != dataset["revision"]:
                raise PreparationError("dataset_not_active")
            ops = inspect_workers(profiles, schedules, ids, revision, dataset, profile)
            for worker_id, worker_profile, schedule, desired_profile, desired_schedule in ops:
                if not worker_profile["revision"]:
                    payload = {**desired_profile, "expected_revision": 0,
                               "expected_catalog_revision": revision, "reason": SOURCE}
                    worker_profile = unpack(await client.patch(f"/v1/admin/workers/{worker_id}/profile", json=payload), 200)
                if not schedule["configured"]:
                    payload = {**desired_schedule, "expected_revision": schedule["revision"],
                               "expected_profile_revision": worker_profile["revision"]}
                    unpack(await client.put(f"/v1/worker-schedules/{worker_id}", json=payload), 200)
    finally:
        await database.close()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or not sys.argv[1].isascii() or not sys.argv[1].isdecimal():
        raise SystemExit("owned demo API port required")
    profile = sys.argv[2] if len(sys.argv) == 3 else None
    profile_spec(profile)
    asyncio.run(prepare_owned_demo(Settings(_env_file=None), int(sys.argv[1]), profile))

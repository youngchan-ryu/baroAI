"""Transactional location mutations; callers own authentication and ticket locks."""

import hashlib
import json
import math
from copy import deepcopy

from fastapi import HTTPException

from app.schemas import LocationView
from app.services.location import registered, resolve
from app.services.routing import suggest
from app.services.safety import has_current_hazard, inspect_raw_text, rule_hit_id
from app.tickets import transition


def check_version(ticket, expected):
    if ticket.location_version != expected:
        raise HTTPException(409, "location_version_conflict")


def preserve_legacy_input(ticket):
    if not ticket.location_history:
        ticket.location_history = [{"kind": "initial", "text": ticket.location_text}]


def safety_evidence_parts(ticket):
    # Every stored location description/answer remains evidence, even after correction.
    location_history = ticket.location_history or [{"text": ticket.location_text}]
    return [ticket.description] + [
        item["text"] for item in location_history if item.get("text")
    ] + list(getattr(ticket, "information_safety_evidence", None) or [])


def refresh_safety(ticket):
    parts = safety_evidence_parts(ticket)
    source = "\n".join(parts)
    if len(source) > 20000:
        raise HTTPException(422, "location_history_limit")
    hits, offset = [], 0
    for part in parts:
        result = inspect_raw_text(part)
        hits.extend(
            {**hit, "start": hit["start"] + offset, "end": hit["end"] + offset,
             "hit_id": rule_hit_id(hit["rule_id"], hit["start"] + offset,
                                   hit["end"] + offset)}
            for hit in result.hits
        )
        offset += len(part) + 1
    floor = min((hit["suggested_urgency"] for hit in hits), default=3)
    previous = getattr(ticket, "safety_data", None) or {}
    # A stored AI judgment survives new evidence; the rules can only make it more urgent.
    ai_urgency = previous.get("ai_urgency")
    if type(ai_urgency) is not int or ai_urgency not in range(4):
        ai_urgency = None
    previous_assessment = previous.get("assessment_status")
    # Preserve the live decision for already assessed evidence. Newly added text
    # receives an independent rule check and can make the decision more urgent.
    added_floor = None
    if ai_urgency is not None and previous_assessment == "live_priority":
        previous_source = previous.get("source_text")
        if isinstance(previous_source, str) and source.startswith(previous_source):
            added_floor = inspect_raw_text(source[len(previous_source):]).rule_floor
        else:
            added_floor = floor  # Unknown history: preserve conservative compatibility.
        automatic = min(previous.get("automatic_urgency", ai_urgency), added_floor)
    else:
        automatic = min(ai_urgency if ai_urgency is not None else 3, floor)
    if ai_urgency is None:
        assessment = "rules_only"
    elif previous_assessment in {"live_with_rules", "replay_with_rules", "live_priority"}:
        assessment = previous_assessment
    else:
        assessment = "live_with_rules" if previous.get("ai_draft") else "replay_with_rules"
    safety = {
        "source_text": source,
        "source_text_hash": hashlib.sha256(source.encode()).hexdigest(),
        "original_text_hash": hashlib.sha256(ticket.description.encode()).hexdigest(),
        "rules_version": result.rule_version,
        "rule_hits": hits,
        "rule_floor": floor,
        "ai_urgency": ai_urgency,
        "automatic_urgency": automatic,
        "assessment_status": assessment,
        "requires_human_review": True,
        "emergency_banner": has_current_hazard(source, hits),
        "rule_disagreement": previous.get("rule_disagreement", False),
        "rule_guard_kept": previous.get("rule_guard_kept", False),
    }
    if isinstance(previous.get("ai_draft"), dict):
        safety["ai_draft"] = previous["ai_draft"]
    if previous.get("booth_live_intake") is True:
        safety["booth_live_intake"] = True
    previous_floor = previous.get("rule_floor", 3)
    if type(previous_floor) is not int:
        previous_floor = 3
    ticket.safety_data = safety
    ticket.safety_version += 1
    effective = getattr(ticket, "effective_urgency", None)
    if (
        ticket.override_reason is not None
        and type(effective) is int
        and ((added_floor if added_floor is not None else
              (floor if floor < previous_floor else 3)) >= effective)
    ):
        # Only a newly more urgent raw rule can release a human decision.
        # An AI result alone never silently releases it on a later refresh.
        return
    if ticket.override_reason is not None:
        # New, more urgent evidence releases the human override visibly, never silently.
        released = {
            "reason": ticket.override_reason,
            "previous_effective": effective,
            "previous_rule_floor": previous_floor,
            "rule_floor": floor,
            "automatic": automatic,
            "assessment_version": ticket.safety_version,
        }
        _record_override_release(ticket, released)
    ticket.effective_urgency = automatic
    ticket.override_reason = None


def _record_override_release(ticket, detail):
    """System audit event in the caller's session; no human actor is implied."""
    from sqlalchemy.orm import object_session

    from app.models import TicketEvent
    from app.security import now

    session = object_session(ticket)
    if session is None:
        # Unpersisted tickets cannot carry an override; refuse rather than lose evidence.
        raise HTTPException(409, "urgency_override_release_unrecorded")
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=None,
            created_at=now(),
            event_type="urgency_override_released",
            reason=json.dumps(
                {"message": "새 위험 신호로 긴급도 조정 해제 · 사람 확인 필요", **detail},
                ensure_ascii=False,
            ),
            from_status=ticket.status,
            to_status=ticket.status,
        )
    )


def save_location(ticket, location):
    ticket.location_version += 1
    location.update(
        version=ticket.location_version, clarification_count=ticket.location_answer_count
    )
    location["can_answer"] = ticket.location_answer_count < 2 and location["state"] not in {
        "confirmed",
        "staff_review",
    }
    ticket.location_data = location
    ticket.routing_data = suggest(
        ticket.category_code, location, ticket.safety_data, ticket.catalog_snapshot
    )


def initialize_location(ticket, payload):
    ticket.location_history = [{"kind": "initial", "text": payload.location_text}]
    ticket.location_answer_count = 0
    ticket.location_version = 0
    ticket.safety_version = 0
    refresh_safety(ticket)
    save_location(
        ticket, resolve(payload.text, payload.location_text, payload.loc_hint, payload.gps)
    )


def confirm_service_location(db, ticket, principal, payload, provider, revision, catalog=None):
    """Caller owns the dispatch gate, ticket lock, and active relationship checks."""
    check_version(ticket, payload.expected_location_version)
    if payload.dataset_hash != provider.sha256 or payload.dataset_revision != revision:
        raise HTTPException(409, "planning_dataset_changed")
    selected = next((loc for loc in provider.dataset.locations if loc.code == payload.code), None)
    if catalog is not None:
        from types import SimpleNamespace

        entry = next(
            (loc for loc in catalog.content["locations"] if loc["code"] == payload.code), None
        )
        selected = SimpleNamespace(code=entry["code"], label=entry["name"]) if entry else None
    if selected is None:
        raise HTTPException(422, "unregistered_planning_node")
    preserve_legacy_input(ticket)
    before = deepcopy(ticket.location_data)
    location = LocationView(
        state="confirmed",
        selected={
            "code": selected.code,
            "label": selected.label,
            "building_code": selected.code,
            "sources": ["dispatcher"],
        },
        dictionary_version=provider.dataset.version,
        source_type="service",
        dataset_hash=provider.sha256,
        dataset_revision=revision,
    ).model_dump()
    save_location(ticket, location)
    transition(
        db,
        ticket,
        principal.user,
        "location_updated",
        json.dumps(
            {"reason": payload.reason, "before": before, "after": ticket.location_data},
            ensure_ascii=False,
        ),
    )


def update_location(db, ticket, principal, payload):
    dispatcher = principal.user.role == "dispatcher"
    if dispatcher and not payload.reason:
        raise HTTPException(422, "reason_required")
    check_version(ticket, payload.expected_version)
    current = LocationView.model_validate(ticket.location_data).model_dump()
    if not dispatcher and (current["state"] in {"confirmed", "staff_review"}):
        raise HTTPException(409, "location_locked")
    preserve_legacy_input(ticket)
    before = {
        "location": deepcopy(current),
        "routing": deepcopy(ticket.routing_data),
        "safety_version": ticket.safety_version,
        "override_reason": ticket.override_reason,
    }
    if payload.action == "confirm":
        selected = next(
            (c for c in current["candidates"] if c["code"] == payload.candidate_code), None
        )
        if not selected or not registered(selected["code"]):
            raise HTTPException(422, "invalid_location_candidate")
        current.update(state="confirmed", selected=selected, question=None)
    elif payload.action == "answer":
        if not payload.answer:
            raise HTTPException(422, "location_answer_required")
        if not dispatcher and ticket.location_answer_count >= 2:
            raise HTTPException(409, "location_answer_limit")
        ticket.location_answer_count += 1
        ticket.location_history = ticket.location_history + [
            {"kind": "answer", "text": payload.answer}
        ]
        ticket.location_text = payload.answer
        current = resolve(location_text=payload.answer)
        if ticket.location_answer_count >= 2 and not dispatcher and current["state"] != "suggested":
            current["state"] = "staff_review"
        refresh_safety(ticket)
    else:
        current.update(state="staff_review", selected=None, question=None)
    save_location(ticket, current)
    # Immutable before/after evidence, excluding coordinates and secrets.
    reason = json.dumps(
        {
            "reason": payload.reason or "신고자 위치 확인",
            "action": payload.action,
            "before": before,
            "after": {
                "location": ticket.location_data,
                "routing": ticket.routing_data,
                "safety_version": ticket.safety_version,
            },
        },
        ensure_ascii=False,
    )
    transition(db, ticket, principal.user, "location_updated", reason)


def catalog_candidates_with_qr(content, text, location_text, hint):
    """Resolve only against the current catalog; a QR is an unconfirmed signal."""
    import re

    from app.catalog import school_location_candidates

    candidates = school_location_candidates(content, text, location_text)
    for candidate in candidates:
        candidate["sources"] = ["text"]
    location = next((item for item in content.get("locations", [])
                     if isinstance(hint, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9:_-]{0,29}", hint)
                     and item.get("code") == hint), None)
    if location:
        qr = next((item for item in candidates if item["code"] == hint), None)
        if qr:
            candidates.remove(qr)
            qr["sources"].insert(0, "qr_hint")
        else:
            qr = {"code": location["code"], "label": location["name"],
                  "building_code": location["building_code"],
                  "floor": None if location["floor"] == "미확인" else str(location["floor"]),
                  "space_code": location["space_code"], "sources": ["qr_hint"]}
        candidates.insert(0, qr)
    return candidates, bool(location)


async def record_catalog_qr(db, ticket, hint):
    from app.models import SchoolCatalogActive, SchoolCatalogVersion

    if hint is None:
        return
    pointer = await db.get(SchoolCatalogActive, 1)
    catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
    _, valid = catalog_candidates_with_qr(catalog.content if catalog else {}, "", "", hint)
    entry = (
        {"kind": "qr_hint", "code": hint, "catalog_revision": catalog.revision}
        if valid else {"kind": "qr_hint_unknown"}
    )
    ticket.location_history = ticket.location_history + [entry]


def nearby_catalog_candidates(content, latitude, longitude, max_distance=300):
    """Return only codes and labels; coordinates and distances stay transient."""
    import math

    nearby = []
    for loc in content.get("locations", []):
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if loc.get("coordinate_system") != "WGS84" or lat is None or lon is None:
            continue
        p1, p2 = math.radians(latitude), math.radians(lat)
        dlat, dlon = p2 - p1, math.radians(lon - longitude)
        a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
        distance = 6371000 * 2 * math.asin(min(1, math.sqrt(a)))
        if distance <= max_distance:
            nearby.append((distance, loc["code"], loc))
    nearby.sort(key=lambda item: (item[0], item[1]))
    result, buildings = [], set()
    for _, _, loc in nearby:
        if loc["building_code"] in buildings:
            continue
        buildings.add(loc["building_code"])
        result.append({"code": loc["code"], "label": loc["name"],
                       "building_code": loc["building_code"],
                       "floor": None if loc["floor"] == "미확인" else str(loc["floor"]),
                       "space_code": loc["space_code"]})
        if len(result) == 3:
            break
    return result


def merge_signal_candidates(candidates, additions, source):
    for addition in additions:
        existing = next((item for item in candidates if item["code"] == addition["code"]), None)
        if existing:
            if source not in existing["sources"]:
                existing["sources"].append(source)
        else:
            candidates.append({**addition, "sources": [source]})
    return candidates

async def photo_location_signal(db, raw):
    from app.models import SchoolCatalogActive, SchoolCatalogVersion
    from app.storage import exif_gps

    point = exif_gps(raw)
    if point is None or db.info["settings"].planning_dataset_provider != "db":
        return None
    pointer = await db.get(SchoolCatalogActive, 1)
    catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
    if not catalog:
        return None
    candidates = nearby_catalog_candidates(catalog.content, *point,
        getattr(db.info["settings"], "location_max_distance_m", 300))
    if not candidates:
        return None
    return {"catalog_revision": catalog.revision, "codes": [item["code"] for item in candidates]}


async def preview_service_location(db, payload, stamp):
    """Transient point-distance reference; never determines a building boundary."""
    from app.models import SchoolCatalogActive, SchoolCatalogVersion
    from app.schemas import GPSComparison, LocationView

    pointer = (
        await db.get(SchoolCatalogActive, 1)
        if db.info["settings"].planning_dataset_provider == "db" else None
    )
    catalog = await db.get(SchoolCatalogVersion, pointer.catalog_revision) if pointer else None
    locations = (catalog.content.get("locations") or []) if catalog else []
    candidates, qr_valid = catalog_candidates_with_qr(
        catalog.content if catalog else {}, payload.text, payload.location_text, payload.loc_hint
    )
    gps = payload.gps
    if gps is None:
        quality = "absent"
    elif gps.timestamp_ms is None:
        quality = "missing_time"
    elif gps.timestamp_ms > stamp.timestamp() * 1000 + 30_000:
        quality = "future"
    elif gps.timestamp_ms < stamp.timestamp() * 1000 - 300_000:
        quality = "stale"
    elif gps.accuracy > getattr(db.info["settings"], "location_gps_max_accuracy_m", 150):
        quality = "inaccurate"
    else:
        quality = "ready"
    by_code = {location["code"]: location for location in locations}
    usable = [
        item for item in candidates
        if by_code[item["code"]].get("coordinate_system") == "WGS84"
        and by_code[item["code"]].get("latitude") is not None
        and by_code[item["code"]].get("longitude") is not None
    ]
    availability = ("no_catalog" if not locations else "no_match" if not candidates else
                    "available" if usable else "no_coordinates")
    points = []
    if quality == "ready":
        for item in usable:
            location = by_code[item["code"]]
            p1, p2 = math.radians(gps.latitude), math.radians(location["latitude"])
            dlat, dlon = p2 - p1, math.radians(location["longitude"] - gps.longitude)
            a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
            points.append({"code": item["code"], "label": item["label"],
                           "distance_m": round(6371000 * 2 * math.asin(min(1, math.sqrt(a))))})
        additions = nearby_catalog_candidates(
            catalog.content if catalog else {}, gps.latitude, gps.longitude,
            getattr(db.info["settings"], "location_max_distance_m", 300),
        )
        merge_signal_candidates(candidates, additions, "current_gps")
    # Only catalog representative points are returned; browser and photo GPS stay transient.
    for candidate in candidates:
        location = by_code.get(candidate["code"], {})
        lat, lon = location.get("latitude"), location.get("longitude")
        if (location.get("coordinate_system") == "WGS84"
                and isinstance(lat, (int, float)) and not isinstance(lat, bool)
                and isinstance(lon, (int, float)) and not isinstance(lon, bool)
                and math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            candidate.update(latitude=lat, longitude=lon)
    return LocationView(
        state="needs_confirmation" if candidates else "staff_review",
        source_type="catalog" if locations else "unregistered",
        dictionary_version=(
            f"school-catalog-{catalog.revision}" if catalog else "service-unregistered-v1"
        ),
        catalog_revision=catalog.revision if catalog else None,
        can_answer=False,
        candidates=candidates,
        issues=["qr_text_conflict"] if qr_valid and any(
            "text" in item["sources"]
            and item["building_code"] != candidates[0]["building_code"]
            for item in candidates
        ) else [],
        gps_comparison=GPSComparison(
            quality=quality, availability=availability,
            catalog_revision=catalog.revision if catalog else None,
            accuracy_m=gps.accuracy if gps else None, points=points,
        ),
    )

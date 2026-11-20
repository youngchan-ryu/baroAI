"""Read-only projections and stable ordering for assignment candidates."""

from types import SimpleNamespace

from app.catalog import CATEGORY_MAP
from app.services.location import registered


def mapped_department(content, category_code, location_code):
    mappings = [m for m in content["mappings"] if m["category_code"] == category_code]
    mapping = next((m for m in mappings if m["location_code"] == location_code), None)
    mapping = mapping or next((m for m in mappings if not m["location_code"]), None)
    if not mapping or mapping["department_code"] not in {
        d["code"] for d in content["departments"]
    }:
        return None
    return mapping["department_code"]


def project_ticket(ticket, settings, catalog, active, category_code, location_code, dataset_codes):
    category_code = category_code or ticket.category_code
    selected = (ticket.location_data or {}).get("selected") or {}
    explicit_location = bool(location_code)
    if not location_code and (ticket.location_data or {}).get("state") != "confirmed":
        return None, "atomic_location_category_unconfirmed", None
    location_code = location_code or selected.get("code")
    if settings.planning_dataset_provider == "db":
        if not catalog or not active or not active.dataset_hash or not active.dataset_revision:
            return None, "school_catalog_unavailable", None
        content = catalog.content
        category = next((x for x in content["categories"] if x["code"] == category_code), None)
        location = next((x for x in content["locations"] if x["code"] == location_code), None)
        if not category or not location:
            return None, "atomic_location_category_unconfirmed", None
        old_location = ticket.location_data or {}
        same_confirmed = (
            old_location.get("state") == "confirmed" and selected.get("code") == location_code
        )
        location_changed = explicit_location and (
            not same_confirmed or old_location.get("source_type") == "catalog"
        )
        if not location_changed and (
            old_location.get("state") != "confirmed"
            or old_location.get("source_type") != "service"
            or old_location.get("dataset_hash") != active.dataset_hash
            or old_location.get("dataset_revision") != active.dataset_revision
        ):
            return None, "atomic_location_category_unconfirmed", category["field_code"]
        department = mapped_department(content, category_code, location_code)
        if department is None:
            return None, "atomic_location_category_unconfirmed", category["field_code"]
        changed = category_code != ticket.category_code
        snapshot = (
            {
                "category_code": category_code,
                "domain": category["field_code"],
                "catalog_backed": True,
            }
            if changed
            else dict(ticket.catalog_snapshot or {})
        )
        if changed or location_changed:
            snapshot["department_code"] = department
        projected = SimpleNamespace(
            category_code=category_code,
            catalog_revision=catalog.revision if changed else ticket.catalog_revision,
            catalog_snapshot=snapshot,
            location_data={
                "state": "confirmed",
                "source_type": "service",
                "dataset_hash": active.dataset_hash,
                "dataset_revision": active.dataset_revision,
                "selected": {"code": location_code},
            },
        )
        return projected, None, category["field_code"]
    category = CATEGORY_MAP.get(category_code)
    if (
        settings.deployment_mode != "demo"
        or not category
        or category_code == "UNKNOWN"
        or not location_code
        or not registered(location_code)
    ):
        return None, "atomic_location_category_unconfirmed", None
    projected = SimpleNamespace(
        category_code=category_code,
        location_data={"state": "confirmed", "selected": {"code": location_code}},
    )
    return projected, None, category["domain"]


def order_candidates(candidates):
    candidates.sort(
        key=lambda c: (
            not c["eligible"],
            c.get("has_ongoing_visit", False),
            c["open_assignments"],
            c.get("estimated_travel_min") is None,
            c.get("estimated_travel_min") if c.get("estimated_travel_min") is not None else 0,
            not c["same_building"] if c.get("estimated_travel_min") is None else False,
            not c["same_region"] if c.get("estimated_travel_min") is None else False,
            c["display_name"].casefold(),
            str(c["worker_id"]),
        )
    )
    rank = 0
    for item in candidates:
        if item["eligible"]:
            rank += 1
            item["rank"] = rank
        else:
            item["rank"] = None
    return candidates

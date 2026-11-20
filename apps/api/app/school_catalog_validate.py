"""Validate school workbook rows without trusting codes, references, or matrix values."""

import hashlib
import json
import math
import re
from datetime import date

from app.planning.datasets import MAX_DB_BUNDLE_BYTES, load_pinned_dataset

CODE = re.compile(r"^[A-Z0-9][A-Z0-9:_-]{0,29}$")
ENTITIES = ("departments", "fields", "categories", "locations", "mappings")
MAX_ERRORS = 1000
MESSAGES = {
    "invalid_code": "코드는 영문 대문자, 숫자와 : _ - 를 사용해 30자 이내로 입력해 주세요.",
    "duplicate_code": "중복된 코드예요.",
    "reserved_code": "UNKNOWN은 미분류를 위한 예약 코드예요.",
    "required": "필수 값을 입력해 주세요.",
    "unknown_reference": "참조하는 코드가 등록되지 않았어요.",
    "invalid_coordinates": "위도와 경도를 모두 비우거나 유효한 좌표로 입력해 주세요.",
    "invalid_coordinate_system": "좌표계는 WGS84 또는 빈칸만 허용해요.",
    "invalid_aliases": "별칭을 텍스트로 입력해 주세요.",
    "ambiguous_mapping": "같은 분류와 위치의 연결이 중복됐어요.",
    "too_many_locations": "위치는 최대 1,000개까지 등록할 수 있어요.",
    "one_dataset_required": "이동시간이 있으면 자료 정보는 한 행이어야 해요.",
    "invalid_date": "날짜를 YYYY-MM-DD 형식으로 입력해 주세요.",
    "duplicate_pair": "같은 출발·도착 위치가 중복됐어요.",
    "invalid_minutes": "이동시간은 0~100000의 정수이며 같은 위치는 0이어야 해요.",
    "incomplete_matrix": "모든 위치 쌍의 이동시간이 필요해요.",
    "invalid_dataset": "계획 자료의 형식이나 크기를 확인해 주세요.",
    "travel_times_required": "이동시간이 없으면 자료 정보 시트도 비워 주세요.",
    "too_many_errors": "오류가 많아 일부만 표시했어요. 수정 후 다시 업로드해 주세요.",
}


def validate(rows, previous=None, travel_settings=None):
    errors = []
    candidate = {key: [] for key in ENTITIES}

    def error(sheet, row, column, code):
        if len(errors) < MAX_ERRORS:
            errors.append(
                {
                    "sheet": sheet,
                    "row": row,
                    "column": column,
                    "code": code,
                    "message": MESSAGES[code],
                }
            )
        elif len(errors) == MAX_ERRORS:
            errors.append(
                {
                    "sheet": sheet,
                    "row": row,
                    "column": column,
                    "code": "too_many_errors",
                    "message": MESSAGES["too_many_errors"],
                }
            )

    def code(value):
        return isinstance(value, str) and CODE.fullmatch(value) is not None

    def text(value):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 500

    for sheet in ("departments", "fields", "categories", "locations"):
        seen = set()
        for rowno, item in rows[sheet]:
            key = item["code"]
            if not code(key):
                error(sheet, rowno, "code", "invalid_code")
            elif key in seen:
                error(sheet, rowno, "code", "duplicate_code")
            elif sheet == "categories" and key == "UNKNOWN":
                error(sheet, rowno, "code", "reserved_code")
            seen.add(key)
            if not text(item["name"]):
                error(sheet, rowno, "name", "required")
            candidate[sheet].append(item)
    codes = {
        sheet: {x["code"] for x in candidate[sheet] if code(x["code"])}
        for sheet in ("departments", "fields", "categories", "locations")
    }
    for rowno, item in rows["categories"]:
        if item["field_code"] not in codes["fields"]:
            error("categories", rowno, "field_code", "unknown_reference")
    for rowno, item in rows["locations"]:
        for column in ("building_code", "space_code", "region"):
            if not code(item[column]):
                error("locations", rowno, column, "invalid_code")
        if item["floor"] is None or str(item["floor"]).strip() == "":
            error("locations", rowno, "floor", "required")
        lat, lon = item["latitude"], item["longitude"]
        if (lat is None) != (lon is None) or (
            lat is not None
            and (
                isinstance(lat, bool)
                or isinstance(lon, bool)
                or not isinstance(lat, (int, float))
                or not isinstance(lon, (int, float))
                or not math.isfinite(lat)
                or not math.isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            )
        ):
            error("locations", rowno, "latitude", "invalid_coordinates")
        if item.get("coordinate_system") not in {None, "", "WGS84"}:
            error("locations", rowno, "coordinate_system", "invalid_coordinate_system")
        if item["aliases"] is not None and not isinstance(item["aliases"], str):
            error("locations", rowno, "aliases", "invalid_aliases")
    mapping_keys = set()
    for rowno, item in rows["mappings"]:
        key = (item["category_code"], item["location_code"] or "")
        if key in mapping_keys:
            error("mappings", rowno, "category_code", "ambiguous_mapping")
        mapping_keys.add(key)
        for column, table in (("category_code", "categories"), ("department_code", "departments")):
            if item[column] not in codes[table]:
                error("mappings", rowno, column, "unknown_reference")
        if item["location_code"] and item["location_code"] not in codes["locations"]:
            error("mappings", rowno, "location_code", "unknown_reference")
        candidate["mappings"].append(item)
    if len(codes["locations"]) > 1000:
        error("locations", 0, "code", "too_many_locations")

    matrix_rows = rows["travel_times"]
    estimate_locations = None
    if (
        len(rows["dataset"]) == 1
        and rows["dataset"][0][1].get("source") == "estimated_straight_line"
    ):
        if matrix_rows:
            error("dataset", rows["dataset"][0][0], "source", "invalid_dataset")
        elif len(codes["locations"]) <= 1000 and not errors:
            from app.planning.travel_estimates import estimate_minutes

            eligible = [
                item
                for item in candidate["locations"]
                if item.get("coordinate_system") == "WGS84"
                and item.get("latitude") is not None
                and item.get("longitude") is not None
            ]
            if eligible:
                estimate_locations = {item["code"] for item in eligible}
                matrix_rows = [
                    (0, (a["code"], b["code"], estimate_minutes(a, b, travel_settings)))
                    for a in eligible
                    for b in eligible
                ]
    # Refuse the size before allocating any quadratic data structure.
    if len(codes["locations"]) > 1000:
        matrix_rows = []
    dataset = None
    if matrix_rows:
        if len(rows["dataset"]) != 1:
            error("dataset", 0, "dataset_id", "one_dataset_required")
        else:
            rowno, meta = rows["dataset"][0]
            for field, value in meta.items():
                if not text(value):
                    error("dataset", rowno, field, "required")
            if not code(meta["dataset_id"]):
                error("dataset", rowno, "dataset_id", "invalid_code")
            try:
                date.fromisoformat(meta["confirmed_at"])
            except (TypeError, ValueError):
                error("dataset", rowno, "confirmed_at", "invalid_date")
        ordered = sorted(
            estimate_locations if estimate_locations is not None else codes["locations"]
        )
        indices = {value: index for index, value in enumerate(ordered)}
        matrix = [[None] * len(ordered) for _ in ordered]
        for rowno, item in matrix_rows:
            start, end, minutes = item
            if start not in indices or end not in indices:
                error("travel_times", rowno, "from_location_code", "unknown_reference")
                continue
            i, j = indices[start], indices[end]
            if matrix[i][j] is not None:
                error("travel_times", rowno, "from_location_code", "duplicate_pair")
            elif (
                type(minutes) is not int or not 0 <= minutes <= 100000 or (i == j and minutes != 0)
            ):
                error("travel_times", rowno, "minutes", "invalid_minutes")
            else:
                matrix[i][j] = minutes
        if any(value is None for line in matrix for value in line):
            error("travel_times", 0, "minutes", "incomplete_matrix")
        if not errors:
            by_code = {item["code"]: item for item in candidate["locations"]}
            dataset = {
                "schema_version": 1,
                "dataset_id": meta["dataset_id"],
                "version": meta["version"],
                "time_unit": "minute",
                "locations": [
                    {"code": key, "label": by_code[key]["name"], "region": by_code[key]["region"]}
                    for key in ordered
                ],
                "travel_minutes": matrix,
                "evidence": {
                    key: meta[key]
                    for key in (
                        "source",
                        "source_version",
                        "confirmation_reference",
                        "confirmed_by",
                        "confirmed_at",
                    )
                },
            }
            if estimate_locations is not None:
                from app.planning.travel_estimates import TravelEstimateSettings

                estimate = travel_settings or TravelEstimateSettings()
                dataset["estimate_parameters"] = {
                    "detour_factor": estimate.detour_factor,
                    "speed_kmh": estimate.speed_kmh,
                    "entry_minutes": estimate.entry_minutes,
                }
            raw = json.dumps(
                dataset, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            digest = hashlib.sha256(raw).hexdigest()
            try:
                load_pinned_dataset(raw, digest, max_bytes=MAX_DB_BUNDLE_BYTES)
            except ValueError:
                error("dataset", 0, "dataset_id", "invalid_dataset")
            else:
                dataset = {"canonical": raw.decode(), "sha256": digest}
    elif rows["dataset"]:
        error("dataset", rows["dataset"][0][0], "dataset_id", "travel_times_required")
    candidate["dataset"] = dataset
    changes = {"added": [], "changed": [], "deactivated": []}
    previous = previous or {}
    for sheet in ENTITIES:

        def key(item, sheet=sheet):
            return (
                str(item["code"])
                if sheet != "mappings"
                else f"{item['category_code']}/{item['location_code'] or ''}"
            )

        before = {key(item): item for item in previous.get(sheet, [])}
        after = {key(item): item for item in candidate[sheet]}
        for value in sorted(after.keys() - before.keys()):
            changes["added"].append(
                {"sheet": sheet, "key": value, "before": None, "after": after[value]}
            )
        for value in sorted(before.keys() & after.keys()):
            if before[value] != after[value]:
                changes["changed"].append(
                    {"sheet": sheet, "key": value, "before": before[value], "after": after[value]}
                )
        for value in sorted(before.keys() - after.keys()):
            changes["deactivated"].append(
                {"sheet": sheet, "key": value, "before": before[value], "after": None}
            )
    return candidate, errors, changes

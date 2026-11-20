"""Bounded deterministic lookup in a synthetic demonstration dictionary."""

import math
import re
import unicodedata
from difflib import SequenceMatcher

VERSION = "synthetic-locations-v1"
BUILDINGS = [
    {
        "code": "DEMO-A",
        "label": "합성 가동",
        "aliases": ["합성 가동", "시연 가동"],
        "floors": ["B1", "1", "2", "3"],
        "latitude": 0.0,
        "longitude": 0.0,
        "dorm": False,
    },
    {
        "code": "DEMO-B",
        "label": "합성 나동",
        "aliases": ["합성 나동", "시연 나동"],
        "floors": ["1", "2", "3"],
        "latitude": 0.0,
        "longitude": 0.001,
        "dorm": False,
    },
    {
        "code": "DEMO-DORM",
        "label": "합성 생활관",
        "aliases": ["합성 생활관", "시연 생활관"],
        "floors": ["1", "2"],
        "latitude": 0.001,
        "longitude": 0.0,
        "dorm": True,
    },
]
BY_CODE = {b["code"]: b for b in BUILDINGS}
REGISTERED_SPACES = {
    "DEMO-A:1:LOBBY",
    "DEMO-A:2:WR-F",
    "DEMO-A:2:WR-M",
    "DEMO-A:3:WR-F",
    "DEMO-A:3:WR-M",
    "DEMO-B:1:LOBBY",
    "DEMO-B:2:LOBBY",
    "DEMO-B:3:WR-F",
    "DEMO-DORM:1:LOBBY",
    "DEMO-DORM:2:WR-F",
    "DEMO-DORM:2:WR-M",
}
SPACES = {"LOBBY": "로비", "WR-F": "여자화장실", "WR-M": "남자화장실"}


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def candidate(building, floor=None, space=None, source="text"):
    code = building["code"] + (f":{floor}" if floor else "") + (f":{space}" if space else "")
    return {
        "code": code,
        "label": " ".join(
            filter(None, [building["label"], f"{floor}층" if floor else None, SPACES.get(space)])
        ),
        "building_code": building["code"],
        "floor": floor,
        "space_code": space,
        "sources": [source],
    }


def registered(code, source="hint"):
    bits = code.split(":")
    building = BY_CODE.get(bits[0])
    if not building or len(bits) > 3 or any(not part for part in bits):
        return None
    floor = bits[1] if len(bits) > 1 else None
    space = bits[2] if len(bits) > 2 else None
    if floor and floor not in building["floors"]:
        return None
    if space and (space not in SPACES or not floor or code not in REGISTERED_SPACES):
        return None
    return candidate(building, floor, space, source)


def text_candidates(value, source, issues):
    value = normalize(value)
    explicit = []
    for match in re.finditer(
        r"(?<![A-Z0-9-])DEMO-[A-Z]+(?::[A-Z0-9-]+){0,2}(?![A-Z0-9:-])", value.upper()
    ):
        item = registered(match.group(), source)
        if item:
            explicit.append(item)
        else:
            issues.append("unregistered_space" if ":" in match.group() else "unregistered_building")
    found = [
        b
        for b in BUILDINGS
        if re.search(r"(?<![A-Z0-9-])" + re.escape(b["code"]) + r"(?![A-Z0-9-])", value.upper())
        or any(a in value for a in b["aliases"])
    ]
    fuzzy = False
    if not found and value:
        for building in BUILDINGS:
            if any(
                SequenceMatcher(None, value, alias).ratio() >= 0.8 for alias in building["aliases"]
            ):
                found.append(building)
                fuzzy = True
    floors = re.findall(r"(?:지하\s*(\d{1,2})\s*층?|B\s*(\d{1,2})|(\d{1,2})\s*층)", value, re.I)
    floors = list(dict.fromkeys(f"B{a or b}" if a or b else c for a, b, c in floors))
    spaces = [code for code, label in SPACES.items() if label in value.replace(" ", "")]
    if "화장실" in value and not spaces:
        issues.append("space_ambiguous")
    if re.search(r"\d{3,4}\s*호", value):
        issues.append("unregistered_space")
    if re.search(r"\d{1,3}(?:-\d{1,2})?\s*동", value) and not found:
        issues.append("unregistered_building")
    result = list(explicit)
    for building in found:
        valid = [f for f in floors if f in building["floors"]]
        if len(valid) != len(floors):
            issues.append("unregistered_floor")
        for floor in valid or [None]:
            emitted = False
            for space in spaces if floor else []:
                if f"{building['code']}:{floor}:{space}" in REGISTERED_SPACES:
                    result.append(candidate(building, floor, space, "typo" if fuzzy else source))
                    emitted = True
                else:
                    issues.append("unregistered_space")
            if not emitted:
                result.append(candidate(building, floor, None, "typo" if fuzzy else source))
    return result


def distance(lat, lon, building):
    p1, p2 = math.radians(lat), math.radians(building["latitude"])
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(building["longitude"] - lon) / 2) ** 2
    )
    return 6371000 * 2 * math.asin(min(1, math.sqrt(a)))


def resolve(text="", location_text="", loc_hint=None, gps=None):
    issues, candidates = [], []
    if loc_hint:
        hint = registered(loc_hint)
        if hint:
            candidates.append(hint)
        else:
            issues.append("unregistered_hint")
    candidates += text_candidates(text, "text", issues)
    candidates += text_candidates(location_text, "location_text", issues)
    if gps:
        if gps.accuracy > 50:
            issues.append("gps_low_accuracy")
        nearby = sorted((distance(gps.latitude, gps.longitude, b), b["code"]) for b in BUILDINGS)
        for dist, code in nearby[:2]:
            if dist <= 120:
                candidates.append(candidate(BY_CODE[code], source="gps"))
        if nearby[0][0] > 120:
            issues.append("gps_outside_dictionary")
    merged = {}
    for item in candidates:
        if item["code"] in merged:
            merged[item["code"]]["sources"] = list(
                dict.fromkeys(merged[item["code"]]["sources"] + item["sources"])
            )
        else:
            merged[item["code"]] = item
    candidates = list(merged.values())
    buildings = {c["building_code"] for c in candidates}
    floors = {c["floor"] for c in candidates if c["floor"]}
    spaces = {c["space_code"] for c in candidates if c["space_code"]}
    if len(buildings) > 1 or len(floors) > 1 or len(spaces) > 1:
        issues.append("signal_conflict")
    # Compatible partial signals do not conceal a more detailed registered candidate.
    candidates.sort(key=lambda c: (bool(c["space_code"]), bool(c["floor"])), reverse=True)
    issues = list(dict.fromkeys(issues))
    if not candidates:
        state = "needs_confirmation"
    elif issues or all(set(c["sources"]) <= {"gps", "typo"} for c in candidates):
        state = "needs_confirmation"
    else:
        state = "suggested"
    return {
        "state": state,
        "version": 0,
        "clarification_count": 0,
        "can_answer": True,
        "selected": None,
        "candidates": candidates[:3],
        "issues": issues,
        "question": "위치를 확인해 주세요. 후보를 선택하거나 직접 입력할 수 있어요.",
        "dictionary_version": VERSION,
        "source_type": "synthetic",
    }

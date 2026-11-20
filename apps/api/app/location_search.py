"""Bounded search over the active school catalog, with a minimal response projection."""

import re

from pydantic import BaseModel, Field


class LocationSearchQuery(BaseModel):
    query: str = Field(min_length=1, max_length=100)


class LocationSearchItem(BaseModel):
    code: str
    label: str
    building_code: str
    region: str


class LocationSearchResult(BaseModel):
    revision: int | None
    locations: list[LocationSearchItem]


def _normal(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _natural(value: str) -> list:
    return [
        (0, int(part)) if part.isdecimal() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    ]


def search_locations(rows: list[dict], query: str) -> list[LocationSearchItem]:
    q = _normal(query)
    if not q:
        return []
    found = []
    for row in rows:
        code, label = row.get("code"), row.get("name")
        if not isinstance(code, str) or not isinstance(label, str):
            continue
        building = row.get("building_code") or ""
        region = row.get("region") or ""
        aliases = row.get("aliases") or ""
        if not all(isinstance(x, str) for x in (building, region, aliases)):
            continue
        if not any(q in _normal(x) for x in (code, label, building, region, *aliases.splitlines())):
            continue
        number_match = re.search(r"\d+(?:-\d+)?", label)
        number = number_match.group() if number_match else _normal(building)
        rank = 0 if number == q else 1 if number.startswith(q + "-") else 2
        found.append(
            (
                rank,
                _natural(number),
                _natural(label),
                LocationSearchItem(code=code, label=label, building_code=building, region=region),
            )
        )
    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found[:8]]

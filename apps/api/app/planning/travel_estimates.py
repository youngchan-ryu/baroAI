"""Straight-line planning estimates for catalog locations with WGS84 coordinates."""

import math
from dataclasses import dataclass

SOURCE = "estimated_straight_line"
EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class TravelEstimateSettings:
    detour_factor: float = 1.5
    speed_kmh: float = 10.0
    entry_minutes: float = 4.0

    def __post_init__(self):
        if (
            not all(
                math.isfinite(x) for x in (self.detour_factor, self.speed_kmh, self.entry_minutes)
            )
            or self.detour_factor <= 0
            or self.speed_kmh <= 0
            or self.entry_minutes < 0
        ):
            raise ValueError("invalid_travel_estimate_settings")


def estimate_minutes(start, end, settings=None):
    """Return integer minutes, rounded up; return None without two valid WGS84 points."""
    settings = settings or TravelEstimateSettings()
    if start["code"] == end["code"]:
        return 0
    points = []
    for location in (start, end):
        lat, lon = location.get("latitude"), location.get("longitude")
        if (
            location.get("coordinate_system") != "WGS84"
            or isinstance(lat, bool)
            or isinstance(lon, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lon, (int, float))
            or not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            return None
        points.append((math.radians(lat), math.radians(lon)))
    if start.get("building_code") and start.get("building_code") == end.get("building_code"):
        return 2
    (lat1, lon1), (lat2, lon2) = points
    a = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    distance = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))
    return math.ceil(
        distance * settings.detour_factor / settings.speed_kmh * 60 + settings.entry_minutes
    )

"""Fail-closed validation for an externally pinned planning data bundle.

This module does not install a bundle or establish the truth of its evidence. An
operator-controlled digest must be supplied separately from the bundle; callers
must establish who is authorized to pin it before enabling service planning.
"""

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
Code = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9:_-]{0,99}$")]
Minute = Annotated[int, Field(strict=True, ge=0, le=100000)]
MAX_BUNDLE_BYTES = 2_000_000
MAX_DB_BUNDLE_BYTES = 16_000_000
UNVERIFIED_TRAVEL_SOURCE = "공개자료 기반·기관 미검증"


def has_unverified_travel(provider):
    return provider is not None and provider.dataset.evidence.source == UNVERIFIED_TRAVEL_SOURCE


class Immutable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class Location(Immutable):
    code: Code
    label: Text
    region: Code


class Evidence(Immutable):
    source: Text
    source_version: Text
    confirmation_reference: Text
    confirmed_by: Text
    confirmed_at: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @field_validator("confirmed_at")
    @classmethod
    def calendar_date(cls, value):
        date.fromisoformat(value)
        return value


class TravelEstimateParameters(Immutable):
    detour_factor: Annotated[float, Field(gt=0, le=100, allow_inf_nan=False)]
    speed_kmh: Annotated[float, Field(gt=0, le=500, allow_inf_nan=False)]
    entry_minutes: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)]


class PlanningDataset(Immutable):
    schema_version: Literal[1]
    dataset_id: Code
    version: Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    time_unit: Literal["minute"]
    locations: Annotated[tuple[Location, ...], Field(min_length=1, max_length=1000)]
    travel_minutes: tuple[tuple[Minute, ...], ...]
    evidence: Evidence
    estimate_parameters: TravelEstimateParameters | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("invalid_schema_version")
        return value

    @model_validator(mode="after")
    def validate_matrix_and_codes(self):
        if (self.evidence.source == "estimated_straight_line") != (
            self.estimate_parameters is not None
        ):
            raise ValueError("invalid_estimate_provenance")
        codes = [location.code for location in self.locations]
        size = len(codes)
        if len(set(codes)) != size:
            raise ValueError("duplicate_location")
        if len(self.travel_minutes) != size or any(
            len(row) != size or row[i] != 0 for i, row in enumerate(self.travel_minutes)
        ):
            raise ValueError("invalid_travel_matrix")
        if any(
            value.startswith("DEMO-")
            for value in [self.dataset_id, *codes, *(x.region for x in self.locations)]
        ):
            raise ValueError("synthetic_code_forbidden")
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def load_pinned_dataset(
    content: bytes, expected_sha256: str, *, max_bytes: int = MAX_BUNDLE_BYTES
) -> PlanningDataset:
    """Validate exact bytes against a separately supplied trusted SHA-256 pin.

    Evidence fields are metadata, not proof that any person or institution has
    approved the data. No file path, settings, or deployment is changed here.
    """
    if not isinstance(content, bytes) or not content or len(content) > max_bytes:
        raise ValueError("invalid_bundle_size")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("invalid_dataset_pin")
    if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected_sha256):
        raise ValueError("dataset_hash_mismatch")
    # Detect duplicate keys before Pydantic's JSON parser can collapse them.
    try:
        json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_number")),
        )
        return PlanningDataset.model_validate_json(content)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_dataset") from None


@dataclass(frozen=True, slots=True)
class DatasetProvider:
    """A process-lifetime immutable snapshot; constructing it never schedules reloads."""

    dataset: PlanningDataset
    sha256: str

    @classmethod
    def from_startup_file(cls, path: str, expected_sha256: str):
        """Read a bounded regular file once; path and pin must come from operator config."""
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("invalid_dataset_path")
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("invalid_dataset_file")
            with os.fdopen(fd, "rb") as source:
                fd = None
                content = source.read(MAX_BUNDLE_BYTES + 1)
        except OSError:
            raise ValueError("dataset_file_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)
        return cls(load_pinned_dataset(content, expected_sha256), expected_sha256)

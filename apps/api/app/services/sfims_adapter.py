"""CSV preparation only. Never contacts SFIMS or changes business state."""

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

SCHEMA_VERSION = "baroai-csv-v1"
COLUMNS = [
    "schema_version",
    "data_scope",
    "snapshot_at",
    "snapshot_hash",
    "ticket_created_at",
    "ticket_updated_at",
    "institution_mapping",
    "delivery_status",
    "public_no",
    "status",
    "category_code",
    "domain",
    "suggested_department_code",
    "department_status",
    "location_state",
    "location_source",
    "export_description",
    "export_location",
    "text_source",
    "ai_urgency",
    "own_rule_floor",
    "own_automatic_urgency",
    "linked_rule_floor",
    "own_effective_urgency",
    "manual_override_applied",
    "effective_urgency",
    "safety_review_required",
    "emergency_banner",
]


def validate_text(value):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("export_text_control_or_blank")
    value = value.strip()
    if not value:
        raise ValueError("export_text_control_or_blank")
    if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("export_text_email_forbidden")
    if re.search(
        r"(?i)(https?://|www\.|bearer\s|(?:token|code|otp|csrf|session)[=:]|(?:^|\s)/(?:w|login|auth)(?:[/?#]|$))",
        value,
    ):
        raise ValueError("export_text_auth_or_link_forbidden")
    return value


def safe_cell(value):
    text = "" if value is None else str(value)
    # CSV quoting alone does not stop spreadsheet formula evaluation. Prefix
    # before any leading whitespace/control/format mark can reveal an operator.
    probe = text.lstrip()
    while probe and unicodedata.category(probe[0]).startswith("C"):
        probe = probe[1:].lstrip()
    return "'" + text if probe.startswith(("=", "+", "-", "@")) else text


@dataclass(frozen=True)
class Artifact:
    status: str
    delivery_status: str = "not_sent"
    content: bytes = b""


class SfimsAdapter(Protocol):
    def prepare(self, rows) -> Artifact: ...


class Noop:
    def prepare(self, rows):
        return Artifact("noop")


class CsvExport:
    def prepare(self, rows):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
        return Artifact("generated", content=b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8"))

"""Neutral CSV preparation; no institution mapping or external transmission."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas import Input

Status = Literal[
    "created",
    "needs_info",
    "received",
    "confirmed",
    "assigned",
    "in_progress",
    "completed",
    "transferred",
    "merged",
    "rejected",
    "cancelled",
    "reopened",
]


class ExportFilter(Input):
    from_date: date
    through_date: date
    statuses: list[Status] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def dates(self):
        if (
            self.through_date == date.max
            or not 0 <= (self.through_date - self.from_date).days <= 30
        ):
            raise ValueError("export_date_window_max_31_days")
        return self


class ExportSelection(Input):
    ticket_ids: list[UUID] = Field(default_factory=list, max_length=50)
    filters: ExportFilter | None = None

    @model_validator(mode="after")
    def exclusive(self):
        if bool(self.ticket_ids) == bool(self.filters) or len(set(self.ticket_ids)) != len(
            self.ticket_ids
        ):
            raise ValueError("export_selection_required")
        return self


class ExportText(Input):
    ticket_id: UUID
    export_description: str = Field(min_length=1, max_length=1000)
    export_location: str = Field(min_length=1, max_length=200)

    @field_validator("export_description", "export_location", mode="before")
    @classmethod
    def safe_text(cls, value):
        from app.services.sfims_adapter import validate_text

        return validate_text(value)


class ExportPreviewInput(Input):
    selection: ExportSelection
    rows: list[ExportText] = Field(min_length=1, max_length=50)


class ExportCreate(ExportPreviewInput):
    expected_snapshot_at: datetime
    expected_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed_ticket_ids: list[UUID] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def safe_reason(cls, value):
        from app.services.sfims_adapter import validate_text

        return validate_text(value)


class ExportPreview(BaseModel):
    schema_version: Literal["baroai-csv-v1"] = "baroai-csv-v1"
    adapter: Literal["csv"] = "csv"
    mapping_status: Literal["unverified"] = "unverified"
    delivery_status: Literal["not_sent"] = "not_sent"
    data_scope: Literal["demo", "service"]
    snapshot_hash: str
    snapshot_at: datetime
    columns: list[str]
    ticket_ids: list[UUID]
    rows: list[list[str]]
    warnings: list[str]


class ExportView(BaseModel):
    id: UUID
    adapter_status: Literal["generated"] = "generated"
    delivery_status: Literal["not_sent"] = "not_sent"
    mapping_status: Literal["unverified"] = "unverified"
    schema_version: Literal["baroai-csv-v1"] = "baroai-csv-v1"
    data_scope: Literal["demo", "service"]
    snapshot_hash: str
    content_sha256: str
    row_count: int
    created_at: datetime
    expires_at: datetime
    expired: bool
    download_path: str

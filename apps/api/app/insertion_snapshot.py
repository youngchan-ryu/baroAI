"""Internal service child execution snapshot prototype; not an approval or execution gate.

The independent expected parent and route evidence must come from locked, validated DB
state. This module intentionally does not persist snapshots or enable insertion.
Token and report preservation are not verified by this prototype.
"""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Origin(SnapshotModel):
    assignment_id: UUID
    ticket_id: UUID
    worker_id: UUID
    plan_id: UUID
    sequence: int = Field(ge=0, strict=True)
    location_code: str = Field(min_length=1, max_length=100)
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_revision: int = Field(ge=1, strict=True)
    planned_arrival: AwareDatetime
    planned_completion: AwareDatetime

    @model_validator(mode="after")
    def valid_times(self):
        if self.planned_completion < self.planned_arrival:
            raise ValueError("invalid_planned_times")
        return self


class Entry(SnapshotModel):
    origin: Origin
    classification: Literal["history", "fixed", "route", "excluded"]
    execution_order: int = Field(ge=0, strict=True)
    predecessor_id: UUID | None
    parent_membership_id: UUID | None
    replaces_assignment_id: UUID | None = None


class Disposition(SnapshotModel):
    parent_assignment_id: UUID
    parent_membership_id: UUID
    classification: Literal["replaced", "unassigned"]
    replacement_assignment_id: UUID | None = None


class WorkerPath(SnapshotModel):
    worker_id: UUID
    entries: tuple[Entry, ...]


class ExecutionSnapshot(SnapshotModel):
    version: Literal[1]
    plan_id: UUID
    parent_plan_id: UUID
    dataset_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_revision: int = Field(ge=1, strict=True)
    workers: tuple[WorkerPath, ...]
    dispositions: tuple[Disposition, ...]

    @field_validator("version", mode="before")
    @classmethod
    def valid_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("snapshot_version_invalid")
        return value


class ParentEvidence(SnapshotModel):
    membership_id: UUID
    origin: Origin
    classification: Literal["history", "fixed", "route", "excluded"]
    execution_order: int = Field(ge=0, strict=True)
    predecessor_id: UUID | None


class RouteEvidence(SnapshotModel):
    worker_id: UUID
    assignment_id: UUID
    ticket_id: UUID
    location_code: str = Field(min_length=1, max_length=100)
    origin_sequence: int = Field(ge=0, strict=True)
    planned_arrival: AwareDatetime
    planned_completion: AwareDatetime


def validate_execution_snapshot(
    snapshot: ExecutionSnapshot,
    *,
    parent: tuple[ParentEvidence, ...],
    routes: tuple[RouteEvidence, ...],
    parent_plan_id: UUID,
    plan_id: UUID,
    dataset_hash: str,
    dataset_revision: int,
) -> None:
    """Cross-check complete parent coverage and solver order against independent evidence.

    Historical/fixed/excluded state, membership ownership, result validity, and origin
    evidence must be independently established by the caller under the dispatch gate.
    This validator does not attest those facts or make a snapshot immutable in storage.
    """
    if (
        snapshot.parent_plan_id != parent_plan_id
        or snapshot.plan_id != plan_id
        or snapshot.dataset_hash != dataset_hash
        or snapshot.dataset_revision != dataset_revision
        or parent_plan_id == plan_id
    ):
        raise ValueError("snapshot_identity_invalid")
    parents = {p.origin.assignment_id: p for p in parent}
    if len(parents) != len(parent) or len({p.membership_id for p in parent}) != len(parent):
        raise ValueError("parent_evidence_duplicate")
    parent_paths = {}
    for item in parent:
        parent_paths.setdefault(item.origin.worker_id, []).append(item)
    for items in parent_paths.values():
        items.sort(key=lambda item: item.execution_order)
        previous = None
        stage = -1
        fixed_count = 0
        route_seen = excluded_seen = False
        for order, item in enumerate(items):
            rank = {"history": 0, "fixed": 1, "route": 2, "excluded": 2}[item.classification]
            fixed_count += item.classification == "fixed"
            route_seen |= item.classification == "route"
            excluded_seen |= item.classification == "excluded"
            if (
                item.execution_order != order
                or item.predecessor_id != previous
                or rank < stage
                or fixed_count > 1
                or (route_seen and excluded_seen)
            ):
                raise ValueError("parent_execution_path_invalid")
            stage = rank
            previous = item.origin.assignment_id
    if len({path.worker_id for path in snapshot.workers}) != len(snapshot.workers):
        raise ValueError("worker_duplicate")
    entries = [entry for path in snapshot.workers for entry in path.entries]
    by_id = {entry.origin.assignment_id: entry for entry in entries}
    if len(by_id) != len(entries) or len({entry.origin.ticket_id for entry in entries}) != len(
        entries
    ):
        raise ValueError("entry_duplicate")
    consumed = set()
    observed_routes = []
    for path in snapshot.workers:
        previous = None
        stage = -1
        fixed_count = 0
        route_seen = excluded_seen = False
        for order, entry in enumerate(path.entries):
            origin = entry.origin
            if (
                entry.execution_order != order
                or entry.predecessor_id != previous
                or origin.worker_id != path.worker_id
                or origin.dataset_hash != dataset_hash
                or origin.dataset_revision != dataset_revision
            ):
                raise ValueError("execution_path_invalid")
            rank = {"history": 0, "fixed": 1, "route": 2, "excluded": 2}[entry.classification]
            if rank < stage:
                raise ValueError("execution_class_order_invalid")
            stage = rank
            fixed_count += entry.classification == "fixed"
            route_seen |= entry.classification == "route"
            excluded_seen |= entry.classification == "excluded"
            if fixed_count > 1 or (route_seen and excluded_seen):
                raise ValueError("execution_path_class_invalid")
            old = parents.get(origin.assignment_id)
            if old:
                if (
                    entry.parent_membership_id != old.membership_id
                    or origin != old.origin
                    or entry.classification != old.classification
                ):
                    raise ValueError("preserved_origin_invalid")
                if entry.replaces_assignment_id is not None:
                    raise ValueError("preserved_replacement_invalid")
                consumed.add(origin.assignment_id)
            else:
                if entry.parent_membership_id is not None or entry.classification != "route":
                    raise ValueError("new_entry_invalid")
                if origin.plan_id != plan_id:
                    raise ValueError("new_origin_invalid")
            if entry.classification == "route":
                observed_routes.append(
                    RouteEvidence(
                        worker_id=path.worker_id,
                        assignment_id=origin.assignment_id,
                        ticket_id=origin.ticket_id,
                        location_code=origin.location_code,
                        origin_sequence=origin.sequence,
                        planned_arrival=origin.planned_arrival,
                        planned_completion=origin.planned_completion,
                    )
                )
            previous = origin.assignment_id
        expected = parent_paths.get(path.worker_id, [])
        for classification in ("history", "excluded"):
            if [
                e.origin.assignment_id for e in path.entries if e.classification == classification
            ] != [p.origin.assignment_id for p in expected if p.classification == classification]:
                raise ValueError("preserved_path_order_invalid")
    dispositions = {item.parent_assignment_id: item for item in snapshot.dispositions}
    if len(dispositions) != len(snapshot.dispositions):
        raise ValueError("disposition_duplicate")
    for assignment_id, item in dispositions.items():
        old = parents.get(assignment_id)
        if (
            not old
            or old.membership_id != item.parent_membership_id
            or old.classification != "route"
            or assignment_id in consumed
        ):
            raise ValueError("disposition_parent_invalid")
        if item.classification == "unassigned":
            if item.replacement_assignment_id is not None:
                raise ValueError("disposition_replacement_invalid")
        else:
            replacement = by_id.get(item.replacement_assignment_id)
            if (
                not replacement
                or replacement.replaces_assignment_id != assignment_id
                or replacement.origin.ticket_id != old.origin.ticket_id
                or replacement.origin.assignment_id in parents
            ):
                raise ValueError("disposition_replacement_invalid")
        consumed.add(assignment_id)
    if consumed != set(parents):
        raise ValueError("parent_coverage_invalid")
    for entry in entries:
        if entry.replaces_assignment_id is not None:
            item = dispositions.get(entry.replaces_assignment_id)
            if not item or item.replacement_assignment_id != entry.origin.assignment_id:
                raise ValueError("replacement_backlink_invalid")
    # Compare per worker so independent workers need no arbitrary global ordering.
    for worker in {r.worker_id for r in routes} | {r.worker_id for r in observed_routes}:
        if [r for r in observed_routes if r.worker_id == worker] != [
            r for r in routes if r.worker_id == worker
        ]:
            raise ValueError("solver_route_mismatch")


def parse_execution_snapshot_http(data, **evidence):
    """Map malformed prototype input to a fixed conflict without exposing details.

    Call only with independently validated evidence acquired under the dispatch lock.
    No current HTTP endpoint invokes this helper.
    """
    try:
        snapshot = ExecutionSnapshot.model_validate(data)
        validate_execution_snapshot(snapshot, **evidence)
        return snapshot
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        raise HTTPException(409, "service_execution_snapshot_invalid") from None

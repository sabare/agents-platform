"""Validated creation/read contracts for the first persisted task type."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecordRequirementInput(Contract):
    record_type: Literal["RECORDS", "BILLS", "IMAGING", "SURGICAL_NOTES"]
    all_dates: bool
    from_date: date | None = None
    through_date: date | None = None

    @model_validator(mode="after")
    def dates_are_explicit(self):
        if self.all_dates:
            if self.from_date is not None or self.through_date is not None:
                raise ValueError("all_dates cannot be combined with a date range")
        elif self.from_date is None or self.through_date is None:
            raise ValueError("provide both dates, or explicitly select all_dates")
        elif self.through_date < self.from_date:
            raise ValueError("through_date must be on or after from_date")
        return self


class CreateRecordsTask(Contract):
    case_id: UUID
    parent_task_id: UUID | None = None
    patient_contact_id: UUID
    provider_contact_id: UUID
    agent_version: int = Field(default=5, gt=0)
    reminder_interval_seconds: int = Field(default=3600, ge=10, le=604800)
    objective: str = Field(min_length=1, max_length=2000)
    requirements: list[RecordRequirementInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_requirements(self):
        values = [item.model_dump_json() for item in self.requirements]
        if len(values) != len(set(values)):
            raise ValueError("duplicate record requirements")
        if self.patient_contact_id == self.provider_contact_id:
            raise ValueError("patient and provider must be different contacts")
        return self


class RequirementView(RecordRequirementInput):
    id: UUID
    status: Literal["RECEIVED", "OUTSTANDING"] = "OUTSTANDING"
    evidence_source_ids: list[UUID] = Field(default_factory=list)


class RecordsRequestView(Contract):
    id: UUID
    patient_contact_id: UUID
    provider_contact_id: UUID
    status: str
    requirements: list[RequirementView]
    reminders_enabled: bool = False
    reminder_count: int = 0
    max_reminders: int = 3
    next_reminder_at: datetime | None = None
    callback_at: datetime | None = None
    callback_timezone: str | None = None


class TaskSnapshot(Contract):
    id: UUID
    tenant_id: UUID
    case_id: UUID
    parent_task_id: UUID | None
    kind: str
    task_type: str
    objective: str
    status: str
    version: int
    agent_key: str | None
    agent_version: int | None
    input_schema: str
    input_json: dict
    progress_json: dict
    completion_json: dict
    workflow_id: str | None
    created_at: datetime


class TaskView(TaskSnapshot):
    records_request: RecordsRequestView | None
    client_check_in: dict | None = None
    check_in_series: dict | None = None
    monthly_check_in_schedule: dict | None = None


class EventView(Contract):
    id: UUID
    event_type: str
    actor_ref: str
    payload: dict
    created_at: datetime


class ProviderUpdate(Contract):
    status: Literal["PENDING", "PARTIAL", "RECEIVED"]
    message: str = Field(min_length=1, max_length=2000)
    received_requirement_ids: list[UUID] | None = Field(default=None, max_length=20)
    callback_at: datetime | None = None
    callback_date: date | None = None
    callback_timezone: str | None = Field(default=None, max_length=100)
    callback_local_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    clear_callback: bool = False

    @model_validator(mode="after")
    def valid_callback(self):
        ids = self.received_requirement_ids
        if ids is not None and (not ids or len(ids) != len(set(ids))):
            raise ValueError("Provide a nonempty list of distinct received requirement IDs")
        if self.status == "PARTIAL" and ids is None:
            raise ValueError("Partial receipt requires explicit received_requirement_ids")
        if self.status == "PENDING" and ids is not None:
            raise ValueError("Use PARTIAL or RECEIVED when attaching received items")
        if self.callback_timezone:
            try:
                ZoneInfo(self.callback_timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(
                    "Use a valid hospital IANA timezone, e.g. America/New_York"
                ) from exc
        if self.callback_at and self.callback_at.tzinfo is None:
            raise ValueError("callback_at requires a timezone offset")
        if self.callback_at and self.callback_date:
            raise ValueError("Provide callback_at or callback_date, not both")
        if self.callback_date and not self.callback_timezone:
            raise ValueError("callback_date requires the hospital timezone")
        if self.clear_callback and (self.callback_at or self.callback_date):
            raise ValueError("Cannot clear and schedule a callback together")
        if self.status == "RECEIVED" and (self.callback_at or self.callback_date):
            raise ValueError("Received records cannot have a pending callback")
        return self


class Observation(Contract):
    statement: str
    source_id: UUID


class NextAction(Contract):
    type: Literal["wait", "complete", "needs_human"]
    follow_up_seconds: int | None
    reason: str


class AgentDecision(Contract):
    task_title: str | None = Field(default=None, min_length=1, max_length=160)
    summary: str
    observations: list[Observation]
    next_action: NextAction


class RecordsDecision(AgentDecision):
    received_source_ids: list[UUID]

"""Validated creation/read contracts for the first persisted task type."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RecordRequirementInput(Contract):
    record_type: Literal["RECORDS", "BILLS", "IMAGING"]
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
    agent_version: int = Field(default=2, gt=0)
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


class RecordsRequestView(Contract):
    id: UUID
    patient_contact_id: UUID
    provider_contact_id: UUID
    status: str
    requirements: list[RequirementView]


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


class EventView(Contract):
    id: UUID
    event_type: str
    actor_ref: str
    payload: dict
    created_at: datetime


class ProviderUpdate(Contract):
    status: Literal["PENDING", "RECEIVED"]
    message: str = Field(min_length=1, max_length=2000)


class Observation(Contract):
    statement: str
    source_id: UUID


class NextAction(Contract):
    type: Literal["wait", "complete", "needs_human"]
    follow_up_seconds: int | None
    reason: str


class AgentDecision(Contract):
    summary: str
    observations: list[Observation]
    next_action: NextAction


class RecordsDecision(AgentDecision):
    received_source_ids: list[UUID]

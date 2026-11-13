"""First task-persistence increment. Memory and execution tables follow separately."""

from datetime import date, datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agents_platform.db import Base


class TenantRow:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("firms.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Firm(Base):
    __tablename__ = "firms"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str]
    timezone: Mapped[str] = mapped_column(default="UTC")


class Contact(TenantRow, Base):
    __tablename__ = "contacts"
    display_name: Mapped[str]
    kind: Mapped[str]
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("kind IN ('PERSON', 'ORGANIZATION')", name="contact_kind"),
    )


class Case(TenantRow, Base):
    __tablename__ = "cases"
    display_number: Mapped[str]
    memory_version: Mapped[int] = mapped_column(default=0, server_default="0")
    title: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "display_number"),
        CheckConstraint("status IN ('OPEN', 'PENDING', 'CLOSED')", name="case_status"),
    )


class CaseContact(TenantRow, Base):
    __tablename__ = "case_contacts"
    case_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    roles: Mapped[list[str]] = mapped_column(ARRAY(String))
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "contact_id"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"]),
        CheckConstraint("cardinality(roles) > 0", name="nonempty_contact_roles"),
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    agent_key: Mapped[str] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    definition_hash: Mapped[str]
    definition_snapshot: Mapped[dict] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (CheckConstraint("version > 0", name="positive_agent_version"),)


class Task(TenantRow, Base):
    __tablename__ = "tasks"
    case_id: Mapped[UUID]
    parent_task_id: Mapped[UUID | None]
    kind: Mapped[str]
    task_type: Mapped[str]
    objective: Mapped[str]
    agent_key: Mapped[str | None]
    agent_version: Mapped[int | None]
    input_schema: Mapped[str]
    input_json: Mapped[dict] = mapped_column(JSONB)
    progress_json: Mapped[dict] = mapped_column(JSONB)
    completion_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="PENDING")
    version: Mapped[int] = mapped_column(default=1)
    workflow_id: Mapped[str | None] = mapped_column(unique=True)
    creation_key: Mapped[str | None]
    creation_hash: Mapped[str | None]
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "creation_key"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "parent_task_id"],
            ["tasks.tenant_id", "tasks.case_id", "tasks.id"],
        ),
        ForeignKeyConstraint(
            ["agent_key", "agent_version"], ["agent_versions.agent_key", "agent_versions.version"]
        ),
        CheckConstraint("parent_task_id IS NULL OR parent_task_id <> id", name="not_own_parent"),
        CheckConstraint("version > 0", name="positive_task_version"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','WAITING','BLOCKED','COMPLETED','FAILED','CANCELLED')",
            name="task_status",
        ),
        CheckConstraint(
            "(kind = 'GROUP' AND agent_key IS NULL AND agent_version IS NULL "
            "AND workflow_id IS NULL) OR (kind = 'AGENT' AND agent_key IS NOT NULL "
            "AND agent_version IS NOT NULL AND workflow_id IS NOT NULL)",
            name="task_assignment",
        ),
        Index("ix_tasks_tenant_case_status", "tenant_id", "case_id", "status"),
    )


class RecordsRequest(TenantRow, Base):
    __tablename__ = "records_requests"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    patient_contact_id: Mapped[UUID]
    provider_contact_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(default="DRAFT")
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "task_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "patient_contact_id"],
            ["case_contacts.tenant_id", "case_contacts.case_id", "case_contacts.contact_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "provider_contact_id"],
            ["case_contacts.tenant_id", "case_contacts.case_id", "case_contacts.contact_id"],
        ),
        CheckConstraint(
            "status IN ('DRAFT','SENT','PENDING','PARTIAL','RECEIVED','CLOSED')",
            name="request_status",
        ),
        CheckConstraint(
            "patient_contact_id <> provider_contact_id", name="distinct_patient_provider"
        ),
    )


class RecordRequirement(TenantRow, Base):
    __tablename__ = "record_requirements"
    case_id: Mapped[UUID]
    request_id: Mapped[UUID]
    record_type: Mapped[str]
    from_date: Mapped[date | None] = mapped_column(Date)
    through_date: Mapped[date | None] = mapped_column(Date)
    all_dates: Mapped[bool] = mapped_column(Boolean)
    required: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "request_id"],
            ["records_requests.tenant_id", "records_requests.case_id", "records_requests.id"],
        ),
        CheckConstraint("record_type IN ('RECORDS','BILLS','IMAGING')", name="record_type"),
        CheckConstraint(
            "(all_dates AND from_date IS NULL AND through_date IS NULL) OR "
            "(NOT all_dates AND from_date IS NOT NULL AND through_date IS NOT NULL "
            "AND through_date >= from_date)",
            name="record_date_range",
        ),
    )


class ActivityEvent(TenantRow, Base):
    __tablename__ = "activity_events"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    event_type: Mapped[str]
    producer: Mapped[str]
    dedupe_key: Mapped[str]
    actor_ref: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "producer", "dedupe_key"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        Index("ix_events_task_time", "tenant_id", "task_id", "created_at"),
    )


class OutboxMessage(TenantRow, Base):
    __tablename__ = "outbox_messages"
    event_id: Mapped[UUID]
    destination: Mapped[str]
    destination_key: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="PENDING")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", "destination", "destination_key"),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"], ["activity_events.tenant_id", "activity_events.id"]
        ),
        CheckConstraint("status IN ('PENDING','DELIVERED','FAILED')", name="outbox_status"),
        CheckConstraint("attempts >= 0", name="nonnegative_delivery_attempts"),
        Index("ix_outbox_pending", "status", "available_at"),
    )


class Source(TenantRow, Base):
    __tablename__ = "sources"
    case_id: Mapped[UUID]
    source_key: Mapped[str]
    kind: Mapped[str]
    content: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "source_key"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
    )


class Instruction(TenantRow, Base):
    __tablename__ = "instructions"
    scope: Mapped[str]
    provider_contact_id: Mapped[UUID | None]
    text: Mapped[str]
    revision: Mapped[int] = mapped_column(default=1)
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_contact_id"], ["contacts.tenant_id", "contacts.id"]
        ),
        CheckConstraint(
            "(scope = 'FIRM' AND provider_contact_id IS NULL) OR "
            "(scope IN ('PROVIDER','FIRM_PROVIDER') AND provider_contact_id IS NOT NULL)",
            name="instruction_scope",
        ),
    )


class InstructionRevision(TenantRow, Base):
    """Published policy history; Instruction remains the current approved projection."""

    __tablename__ = "instruction_revisions"
    instruction_id: Mapped[UUID]
    revision: Mapped[int]
    scope: Mapped[str]
    provider_contact_id: Mapped[UUID | None]
    text: Mapped[str]
    approved_by: Mapped[str]
    reason: Mapped[str]
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "instruction_id"], ["instructions.tenant_id", "instructions.id"]
        ),
        UniqueConstraint("tenant_id", "instruction_id", "revision"),
        CheckConstraint("revision > 0", name="positive_instruction_revision"),
    )


class Execution(TenantRow, Base):
    __tablename__ = "executions"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    cycle: Mapped[int]
    mode: Mapped[str]
    status: Mapped[str]
    attempt: Mapped[int] = mapped_column(default=1)
    context_json: Mapped[dict] = mapped_column(JSONB)
    decision_json: Mapped[dict | None] = mapped_column(JSONB)
    outcome_json: Mapped[dict | None] = mapped_column(JSONB)
    usage_json: Mapped[dict | None] = mapped_column(JSONB)
    error_type: Mapped[str | None]
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "task_id", "cycle"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        Index("ix_executions_task_cycle", "tenant_id", "task_id", "cycle"),
    )


class ToolInvocation(TenantRow, Base):
    __tablename__ = "tool_invocations"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    execution_id: Mapped[UUID]
    tool_name: Mapped[str]
    logical_key: Mapped[str]
    arguments_json: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sql_text("'{}'::jsonb")
    )
    result_json: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "logical_key"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "execution_id"],
            ["executions.tenant_id", "executions.case_id", "executions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
    )


class RecordEvidence(TenantRow, Base):
    __tablename__ = "record_evidence"
    case_id: Mapped[UUID]
    requirement_id: Mapped[UUID]
    source_id: Mapped[UUID]
    __table_args__ = (
        UniqueConstraint("tenant_id", "requirement_id", "source_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "requirement_id"],
            [
                "record_requirements.tenant_id",
                "record_requirements.case_id",
                "record_requirements.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
    )


class TaskInbox(TenantRow, Base):
    __tablename__ = "task_inbox"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    event_id: Mapped[UUID]
    consumed_by_execution_id: Mapped[UUID | None]
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "event_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"], ["activity_events.tenant_id", "activity_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "consumed_by_execution_id"],
            ["executions.tenant_id", "executions.case_id", "executions.id"],
        ),
    )


class CaseCoordination(TenantRow, Base):
    __tablename__ = "case_coordination"
    case_id: Mapped[UUID]
    group_task_id: Mapped[UUID]
    workflow_id: Mapped[str] = mapped_column(unique=True)
    agent_version: Mapped[int] = mapped_column(default=1)
    revision: Mapped[int] = mapped_column(default=1)
    objective_json: Mapped[dict] = mapped_column(JSONB)
    plan_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(default="PENDING")
    stall_count: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "group_task_id"],
            ["tasks.tenant_id", "tasks.case_id", "tasks.id"],
        ),
        CheckConstraint("revision > 0 AND agent_version > 0", name="coordination_versions"),
        CheckConstraint(
            "status IN ('PENDING','MONITORING','NEEDS_REVIEW','SATISFIED','FAILED')",
            name="coordination_status",
        ),
    )


class CaseRestriction(TenantRow, Base):
    """Current case/provider contact hold; original and resolution evidence stay in Sources."""

    __tablename__ = "case_restrictions"
    case_id: Mapped[UUID]
    provider_contact_id: Mapped[UUID | None]
    text: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    revision: Mapped[int] = mapped_column(default=1)
    source_id: Mapped[UUID]
    resolution_source_id: Mapped[UUID | None]
    resolution_reason: Mapped[str | None]
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "provider_contact_id"], ["contacts.tenant_id", "contacts.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "resolution_source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        CheckConstraint(
            "status IN ('OPEN','RESOLVED') AND revision > 0", name="case_restriction_state"
        ),
        Index("ix_case_restrictions_scope", "tenant_id", "case_id", "status"),
    )


class MemoryPassage(TenantRow, Base):
    """Rebuildable source passages, not a second business-state store."""

    __tablename__ = "memory_passages"
    case_id: Mapped[UUID]
    source_id: Mapped[UUID]
    source_hash: Mapped[str]
    position: Mapped[int]
    text: Mapped[str]
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    embedding_attempts: Mapped[int] = mapped_column(default=0)
    embedding_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding_error: Mapped[str | None]
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        UniqueConstraint("tenant_id", "source_id", "position"),
        Index("ix_memory_passages_scope", "tenant_id", "case_id"),
        Index(
            "ix_memory_passages_fts",
            sql_text("to_tsvector('english'::regconfig, text)"),
            postgresql_using="gin",
        ),
    )


class MemorySummary(TenantRow, Base):
    """Versioned, source-linked extractive digest of case history."""

    __tablename__ = "memory_summaries"
    case_id: Mapped[UUID]
    revision: Mapped[int]
    content_hash: Mapped[str]
    content: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        UniqueConstraint("tenant_id", "case_id", "revision"),
    )


class MemoryQuery(TenantRow, Base):
    """Reusable embedding of an exact query, scoped to one firm/case."""

    __tablename__ = "memory_queries"
    case_id: Mapped[UUID]
    query_hash: Mapped[str]
    text: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "case_id"], ["cases.tenant_id", "cases.id"]),
        UniqueConstraint("tenant_id", "case_id", "query_hash"),
    )


class ClientCheckIn(TenantRow, Base):
    __tablename__ = "client_check_ins"
    case_id: Mapped[UUID]
    task_id: Mapped[UUID]
    client_contact_id: Mapped[UUID]
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message: Mapped[str]
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id"),
        UniqueConstraint("tenant_id", "case_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "task_id"], ["tasks.tenant_id", "tasks.case_id", "tasks.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "client_contact_id"],
            ["case_contacts.tenant_id", "case_contacts.case_id", "case_contacts.contact_id"],
        ),
    )


class ClientReport(TenantRow, Base):
    __tablename__ = "client_reports"
    case_id: Mapped[UUID]
    check_in_id: Mapped[UUID]
    client_contact_id: Mapped[UUID]
    source_id: Mapped[UUID]
    previous_report_id: Mapped[UUID | None]
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    report: Mapped[dict] = mapped_column(JSONB)
    changes: Mapped[dict] = mapped_column(JSONB)
    meaningful: Mapped[bool]
    needs_review: Mapped[bool]
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "id"),
        UniqueConstraint("tenant_id", "check_in_id"),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "check_in_id"],
            ["client_check_ins.tenant_id", "client_check_ins.case_id", "client_check_ins.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "client_contact_id"],
            ["case_contacts.tenant_id", "case_contacts.case_id", "case_contacts.contact_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "source_id"],
            ["sources.tenant_id", "sources.case_id", "sources.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id", "previous_report_id"],
            ["client_reports.tenant_id", "client_reports.case_id", "client_reports.id"],
        ),
        Index(
            "ix_client_reports_history", "tenant_id", "case_id", "client_contact_id", "reported_at"
        ),
    )

"""Operator recovery and attributed off-platform contact; no network work in transactions."""

from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, Field, ValidationError, model_validator
from sqlalchemy import func, select

from agents_platform.human_requests import fingerprint, now
from agents_platform.memory import policy_lock
from agents_platform.memory_api import Key, wake_task
from agents_platform.models import (
    ActivityEvent,
    Case,
    CaseContact,
    CaseCoordination,
    Execution,
    ExecutionIncident,
    HumanContinuation,
    ManualContact,
    RecordsRequest,
    Source,
    Task,
    ToolInvocation,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract
from agents_platform.tasks import TaskError

MAX_RECOVERIES = 3


class ContactInput(Contract):
    contact_id: UUID
    occurred_at: AwareDatetime
    channel: Literal["PHONE", "EMAIL", "IN_PERSON", "OTHER"] = "PHONE"
    outcome: Literal["REACHED", "VOICEMAIL", "NO_ANSWER", "WRONG_CONTACT"]
    notes: str = Field(min_length=1, max_length=4000)
    evidence_source_ids: list[UUID] = Field(default_factory=list, max_length=20)
    follow_up_at: AwareDatetime | None = None
    response_due_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_contact(self):
        if self.occurred_at > now() + timedelta(minutes=1):
            raise ValueError("Actual contact time cannot be in the future")
        if bool(self.follow_up_at) != bool(self.response_due_at):
            raise ValueError("Supply both follow-up time and response deadline")
        if self.follow_up_at:
            from agents_platform.human_request_handlers import ScheduleFollowUp

            if self.outcome != "REACHED":
                raise ValueError("An agreed follow-up requires a reached contact")
            ScheduleFollowUp(follow_up_at=self.follow_up_at, response_due_at=self.response_due_at)
        return self


class RecordContact(Contract):
    expected_task_version: int = Field(ge=1)
    contact: ContactInput


class RecoverTask(Contract):
    expected_task_version: int = Field(ge=1)
    expected_incident_version: int = Field(ge=1)
    action: Literal["RETRY", "CANCEL"]
    reason: str = Field(min_length=1, max_length=2000)
    contact: ContactInput | None = None

    @model_validator(mode="after")
    def cancellation(self):
        if self.action == "CANCEL" and self.contact and self.contact.follow_up_at:
            raise ValueError("Cancellation cannot schedule more contact")
        return self


def failure_category(error_type):
    if error_type == "ContextBudgetExceeded":
        return "CONTEXT_BUDGET"
    if error_type == "DeliveryOutcomeUnknown":
        return "OUTCOME_UNKNOWN"
    if any(word in error_type for word in ("Timeout", "Connection", "RateLimit", "APIStatus")):
        return "SERVICE_FAILURE"
    if any(word in error_type for word in ("ValueError", "Validation", "UserError")):
        return "INPUT_OR_EXECUTION"
    return "EXECUTION_FAILURE"


async def ensure_incident(session, task, cycle, error_type="ExecutionRetriesExhausted"):
    generation = task.progress_json.get("recovery_generation", 0)
    row = await session.scalar(
        select(ExecutionIncident).where(
            ExecutionIncident.tenant_id == task.tenant_id,
            ExecutionIncident.task_id == task.id,
            ExecutionIncident.generation == generation,
        )
    )
    if row:
        return row
    row = ExecutionIncident(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        failed_cycle=cycle,
        workflow_id=task.workflow_id,
        generation=generation,
        error_type=error_type[:100],
        category=failure_category(error_type),
        status="OPEN",
        version=1,
    )
    session.add(row)
    await session.flush()
    return row


async def record_context_failure(session, task, execution, usage):
    """Technical capacity failures never authorize contact or create provider reviews."""
    from agents_platform.coordination import enqueue_supervisor
    from agents_platform.human_requests import retire_requests

    incident = await ensure_incident(session, task, execution.cycle, "ContextBudgetExceeded")
    reason = "Required context exceeds its budget. Inspect the execution incident before retrying."
    task.status, task.version = "FAILED", task.version + 1
    task.progress_json = {
        **task.progress_json,
        "last_reason": reason,
        "last_execution_id": str(execution.id),
    }
    outcome = {"status": "FAILED", "reason": reason, "incident_id": str(incident.id)}
    execution.status, execution.error_type = "FAILED", "ContextBudgetExceeded"
    execution.finished_at = now()
    execution.usage_json, execution.outcome_json = usage, outcome
    # Retire only the generic provider review that the old technical-failure path
    # could create. Source-based authorization/communication controls remain intact.
    await retire_requests(session, task, handler="records_follow_up.v1")
    for inbox_id in execution.context_json.get("inbox_ids", []):
        from agents_platform.models import TaskInbox

        entry = await session.get(TaskInbox, UUID(inbox_id))
        if entry and entry.consumed_by_execution_id is None:
            entry.consumed_by_execution_id = execution.id
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        event_type="task.failed",
        producer="context-budget",
        dedupe_key=f"context-budget/{execution.id}",
        actor_ref="platform",
        payload={
            "reason": reason,
            "execution_id": str(execution.id),
            "incident_id": str(incident.id),
            "error_type": "ContextBudgetExceeded",
        },
    )
    session.add(event)
    await enqueue_supervisor(session, event)
    return outcome


async def incident_view(session, row):
    task = await session.get(Task, row.task_id)
    effects = (
        await session.scalars(
            select(ToolInvocation)
            .where(
                ToolInvocation.tenant_id == row.tenant_id,
                ToolInvocation.task_id == row.task_id,
            )
            .order_by(ToolInvocation.created_at)
            .limit(101)
        )
    ).all()
    allowed = ["CANCEL"]
    if row.generation < MAX_RECOVERIES and row.category != "OUTCOME_UNKNOWN":
        allowed.insert(0, "RETRY")
    return {
        "id": str(row.id),
        "task_id": str(row.task_id),
        "case_id": str(row.case_id),
        "objective": task.objective,
        "task_status": task.status,
        "task_version": task.version,
        "failed_cycle": row.failed_cycle,
        "workflow_id": row.workflow_id,
        "generation": row.generation,
        "error_type": row.error_type,
        "category": row.category,
        "status": row.status,
        "version": row.version,
        "allowed_actions": allowed,
        "created_at": row.created_at.isoformat(),
        "resolution": row.resolution,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "saved_actions": [
            {"tool": t.tool_name, "logical_key": t.logical_key, "result": t.result_json}
            for t in effects[:100]
        ],
        "saved_actions_truncated": len(effects) > 100,
        "delivery_note": (
            "The required context could not fit. Inspect the execution's budget diagnostics "
            "and fix context/configuration before retrying. This is not provider approval. "
            "Saved logical actions are reused."
            if row.category == "CONTEXT_BUDGET"
            else "Current outreach is simulated. Saved logical actions are reused. "
            "Unknown external delivery must be reconciled before retry."
        ),
    }


async def lock_task(session, tenant_id, task_id):
    await policy_lock(session, tenant_id)
    case_id = await session.scalar(
        select(Task.case_id).where(
            Task.tenant_id == tenant_id,
            Task.id == task_id,
        )
    )
    if not case_id:
        raise TaskError(404, "Task not found")
    case = await session.scalar(
        select(Case)
        .where(
            Case.tenant_id == tenant_id,
            Case.id == case_id,
        )
        .with_for_update(key_share=True)
    )
    task = await session.scalar(
        select(Task)
        .where(
            Task.tenant_id == tenant_id,
            Task.id == task_id,
        )
        .with_for_update()
    )
    return task, case


async def save_contact(session, task, body, source_key):
    participant = await session.scalar(
        select(CaseContact).where(
            CaseContact.tenant_id == task.tenant_id,
            CaseContact.case_id == task.case_id,
            CaseContact.contact_id == body.contact_id,
        )
    )
    if not participant:
        raise TaskError(422, "Contact must belong to this case")
    if body.evidence_source_ids:
        found = set(
            (
                await session.scalars(
                    select(Source.id).where(
                        Source.tenant_id == task.tenant_id,
                        Source.case_id == task.case_id,
                        Source.id.in_(body.evidence_source_ids),
                    )
                )
            ).all()
        )
        if found != set(body.evidence_source_ids):
            raise TaskError(422, "Supporting evidence must belong to this case")
    # Serialize with automatic client outreach. Actual reached contact affects spacing;
    # voicemail/no-answer never masquerade as a reply or completed check-in.
    if {"CLIENT", "PATIENT"}.intersection(participant.roles):
        from agents_platform.check_in_contact import contact_lock

        await contact_lock(session, task.tenant_id, task.case_id, body.contact_id)
    source = Source(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        source_key=source_key,
        kind="OPERATOR_RECORDED_CONTACT",
        content={
            "contact": body.model_dump(mode="json"),
            "actor": "local-operator",
            "attribution": "Operator-recorded account; provider/client statements are unverified.",
        },
    )
    session.add(source)
    await session.flush()
    row = ManualContact(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        contact_id=body.contact_id,
        occurred_at=body.occurred_at,
        channel=body.channel,
        outcome=body.outcome,
        notes=body.notes,
        actor="local-operator",
        source_id=source.id,
    )
    session.add(row)
    await session.flush()
    if body.follow_up_at:
        from agents_platform.human_request_handlers import (
            apply_records_response,
            ensure_records_request,
        )
        from agents_platform.human_requests import record_decision

        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == task.tenant_id,
                RecordsRequest.task_id == task.id,
            )
        )
        if not request or request.provider_contact_id != body.contact_id:
            raise TaskError(422, "Follow-up scheduling requires this task's medical provider")
        # Use the same explicitly authorized one-action path, without expanding model tools.
        old_status = task.status
        task.status = "BLOCKED"
        human = await ensure_records_request(
            session, task, "Operator recorded an agreed provider follow-up."
        )
        decision = await record_decision(
            session,
            human,
            action="SCHEDULE_FOLLOW_UP",
            payload={
                "follow_up_at": body.follow_up_at.isoformat(),
                "response_due_at": body.response_due_at.isoformat(),
            },
            reason=body.notes,
            source_id=source.id,
            key=source_key,
            body_hash=fingerprint(body.model_dump(mode="json")),
        )
        await apply_records_response(session, task, human, decision)
        human.status, human.resolved_at, human.version = "RESOLVED", now(), human.version + 1
        if old_status == "FAILED":
            task.status = "FAILED"  # Recording facts alone never resumes a failed assignment.
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        event_type="contact.manually_recorded",
        producer="operator-contact",
        dedupe_key=source_key,
        actor_ref="local-operator",
        payload={
            "source_id": str(source.id),
            "contact_id": str(body.contact_id),
            "outcome": body.outcome,
            "occurred_at": body.occurred_at.isoformat(),
            "summary": body.notes,
            "attribution": "Operator-recorded contact",
        },
    )
    session.add(event)
    await session.flush()
    return row, event


async def record_contact(session, tenant_id, task_id, body, key):
    async with session.begin():
        task, case = await lock_task(session, tenant_id, task_id)
        source_key = f"manual-contact/{task_id}/{key}"
        existing = await session.scalar(
            select(Source).where(
                Source.tenant_id == tenant_id,
                Source.source_key == source_key,
            )
        )
        if existing:
            if existing.content["contact"] != body.contact.model_dump(mode="json"):
                raise TaskError(409, "Idempotency-Key used for a different contact")
            return {"source_id": str(existing.id), "replayed": True}
        if case.status != "OPEN" or task.status in ("CANCELLED", "COMPLETED"):
            raise TaskError(409, "Task or case is closed")
        if task.kind != "AGENT" or task.version != body.expected_task_version:
            raise TaskError(409, "Task changed or is not executable; refresh before recording")
        row, event = await save_contact(session, task, body.contact, source_key)
        if task.status == "FAILED":
            from agents_platform.coordination import enqueue_supervisor

            task.version += 1
            await enqueue_supervisor(session, event)
        else:
            await wake_task(session, task, event)
        return {
            "source_id": str(row.source_id),
            "replayed": False,
            "continuation": "Contact recorded. Failed tasks require explicit incident recovery.",
        }


async def resolve_incident(session, tenant_id, incident_id, body, key):
    from agents_platform.coordination import enqueue_supervisor
    from agents_platform.human_requests import retire_requests
    from agents_platform.models import OutboxMessage, TaskInbox

    async with session.begin():
        task_id = await session.scalar(
            select(ExecutionIncident.task_id).where(
                ExecutionIncident.tenant_id == tenant_id,
                ExecutionIncident.id == incident_id,
            )
        )
        if not task_id:
            raise TaskError(404, "Incident not found")
        task, case = await lock_task(session, tenant_id, task_id)
        incident = await session.scalar(
            select(ExecutionIncident)
            .where(
                ExecutionIncident.tenant_id == tenant_id,
                ExecutionIncident.id == incident_id,
            )
            .with_for_update()
        )
        source_key = f"incident/{incident_id}/{key}"
        existing = await session.scalar(
            select(Source).where(
                Source.tenant_id == tenant_id,
                Source.source_key == source_key,
            )
        )
        payload = body.model_dump(mode="json")
        if existing:
            if existing.content["request"] != payload:
                raise TaskError(409, "Idempotency-Key used for a different recovery")
            return {"incident": await incident_view(session, incident), "replayed": True}
        if incident.status != "OPEN" or task.status != "FAILED":
            raise TaskError(409, "Incident is resolved or task is no longer failed")
        if (
            incident.version != body.expected_incident_version
            or task.version != body.expected_task_version
        ):
            raise TaskError(409, "Incident or task changed; refresh before resolving")
        if case.status != "OPEN" and (body.action != "CANCEL" or body.contact is not None):
            raise TaskError(409, "Case is closed")
        if body.action == "RETRY" and (
            incident.generation >= MAX_RECOVERIES or incident.category == "OUTCOME_UNKNOWN"
        ):
            raise TaskError(409, "Retry unavailable: recovery limit or unresolved delivery outcome")
        if body.contact:
            await save_contact(session, task, body.contact, source_key + "/contact")
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            source_key=source_key,
            kind="INCIDENT_RESOLUTION",
            content={"request": payload, "actor": "local-operator"},
        )
        session.add(source)
        await session.flush()
        old_workflow = task.workflow_id
        if body.action == "RETRY":
            maximum_cycle = await session.scalar(
                select(func.max(Execution.cycle)).where(
                    Execution.tenant_id == tenant_id,
                    Execution.task_id == task.id,
                )
            )
            cycle = (
                max(maximum_cycle if maximum_cycle is not None else -1, incident.failed_cycle) + 1
            )
            generation = incident.generation + 1
            task.workflow_id = f"firm/{tenant_id}/task/{task.id}/recovery/{generation}"
            task.progress_json = {
                **task.progress_json,
                "recovery_generation": generation,
                "recovery_cycle": cycle,
                "decision_validation_replans": 0,
                "decision_validation_feedback": None,
                "automatic_activations": 0,
                "last_reason": "Operator recovery queued.",
            }
            task.status = "PENDING"
        else:
            task.status = "CANCELLED"
            task.progress_json = {**task.progress_json, "last_reason": body.reason}
            for row in (
                await session.scalars(
                    select(HumanContinuation).where(
                        HumanContinuation.task_id == task.id,
                        HumanContinuation.status.in_(["PENDING", "APPLIED"]),
                    )
                )
            ).all():
                row.status = "CANCELLED"
        task.version += 1
        incident.status, incident.resolved_at, incident.version = (
            "RESOLVED",
            now(),
            incident.version + 1,
        )
        incident.resolution = {
            "action": body.action,
            "reason": body.reason,
            "actor": "local-operator",
            "source_id": str(source.id),
            "previous_workflow_id": old_workflow,
            "next_workflow_id": task.workflow_id if body.action == "RETRY" else None,
        }
        if body.action == "CANCEL":
            await retire_requests(session, task, "CANCELLED")
        # A technical retry does not resolve business approvals. The runtime
        # rechecks their current subject before reusing or replacing the review.
        coord = await session.scalar(
            select(CaseCoordination)
            .where(
                CaseCoordination.tenant_id == tenant_id,
                CaseCoordination.case_id == task.case_id,
            )
            .with_for_update()
        )
        if coord:
            coord.plan_json = {
                **coord.plan_json,
                "reviews": [
                    r
                    for r in coord.plan_json.get("reviews", [])
                    if r.get("task_id") != str(task.id)
                ],
            }
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            task_id=task.id,
            event_type="incident.resolved",
            producer="operator-recovery",
            dedupe_key=source_key,
            actor_ref="local-operator",
            payload={
                "source_id": str(source.id),
                "incident_id": str(incident.id),
                **incident.resolution,
            },
        )
        session.add(event)
        await session.flush()
        if body.action == "RETRY":
            session.add(
                TaskInbox(
                    tenant_id=tenant_id, case_id=task.case_id, task_id=task.id, event_id=event.id
                )
            )
            session.add(
                OutboxMessage(
                    tenant_id=tenant_id,
                    event_id=event.id,
                    destination="temporal.start_task",
                    destination_key=task.workflow_id,
                    payload={
                        "tenant_id": str(tenant_id),
                        "task_id": str(task.id),
                        "cycle": cycle,
                        "trigger": "recovery",
                        "generation": generation,
                    },
                )
            )
        await enqueue_supervisor(session, event, force=True)
        return {
            "incident": await incident_view(session, incident),
            "replayed": False,
            "continuation": "Recovery saved; execution queued."
            if body.action == "RETRY"
            else "Assignment cancelled.",
        }


router = APIRouter(
    prefix="/v1/firms/{tenant_id}",
    tags=["recovery and manual contact"],
    dependencies=[Depends(require_operator)],
)


@router.get("/incidents")
async def incidents(
    tenant_id: UUID,
    session: Session,
    task_id: UUID | None = None,
    status: Literal["OPEN", "ALL"] = "OPEN",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = select(ExecutionIncident).where(ExecutionIncident.tenant_id == tenant_id)
    if task_id:
        query = query.where(ExecutionIncident.task_id == task_id)
    if status != "ALL":
        query = query.where(ExecutionIncident.status == status)
    rows = (
        await session.scalars(
            query.order_by(ExecutionIncident.created_at.desc(), ExecutionIncident.id)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [await incident_view(session, row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.post("/incidents/{incident_id}/resolve")
async def recover(
    tenant_id: UUID, incident_id: UUID, body: RecoverTask, session: Session, idempotency_key: Key
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        return await resolve_incident(session, tenant_id, incident_id, body, idempotency_key)
    except ValidationError as exc:
        raise HTTPException(422, "Contact or follow-up is no longer valid; refresh") from exc
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/tasks/{task_id}/manual-contacts")
async def contact(
    tenant_id: UUID, task_id: UUID, body: RecordContact, session: Session, idempotency_key: Key
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        return await record_contact(session, tenant_id, task_id, body, idempotency_key)
    except ValidationError as exc:
        raise HTTPException(422, "Contact or follow-up is no longer valid; refresh") from exc
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/tasks/{task_id}/manual-contacts")
async def contacts(
    tenant_id: UUID,
    task_id: UUID,
    session: Session,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    if not await session.scalar(
        select(Task.id).where(Task.tenant_id == tenant_id, Task.id == task_id)
    ):
        raise HTTPException(404, "Task not found")
    rows = (
        await session.scalars(
            select(ManualContact)
            .where(
                ManualContact.tenant_id == tenant_id,
                ManualContact.task_id == task_id,
            )
            .order_by(ManualContact.occurred_at.desc(), ManualContact.id)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "contact_id": str(r.contact_id),
                "occurred_at": r.occurred_at.isoformat(),
                "channel": r.channel,
                "outcome": r.outcome,
                "notes": r.notes,
                "actor": r.actor,
                "source_id": str(r.source_id),
                "recorded_at": r.created_at.isoformat(),
            }
            for r in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


async def backfill_incidents(database):
    async with database.sessions() as session:
        ids = (
            await session.execute(
                select(Task.tenant_id, Task.id).where(
                    Task.kind == "AGENT",
                    Task.status == "FAILED",
                )
            )
        ).all()
    for tenant_id, task_id in ids:
        async with database.sessions.begin() as session:
            task, _ = await lock_task(session, tenant_id, task_id)
            if task.status != "FAILED":
                continue
            execution = await session.scalar(
                select(Execution)
                .where(
                    Execution.tenant_id == tenant_id,
                    Execution.task_id == task_id,
                )
                .order_by(Execution.cycle.desc())
                .limit(1)
            )
            await ensure_incident(
                session,
                task,
                execution.cycle if execution else 0,
                execution.error_type
                if execution and execution.error_type
                else "LegacyExecutionFailure",
            )

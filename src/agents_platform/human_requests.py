"""Shared human work lifecycle. Business decisions are validated by trusted handlers."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, Field, ValidationError
from sqlalchemy import select

from agents_platform.memory import policy_lock
from agents_platform.models import (
    ActivityEvent,
    Case,
    HumanContinuation,
    HumanDecision,
    HumanRequest,
    OutboxMessage,
    Source,
    Task,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract
from agents_platform.tasks import TaskError


def now():
    return datetime.now(timezone.utc)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class HumanResponse(Contract):
    expected_task_version: int = Field(ge=1)
    expected_request_version: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=80)
    payload: dict = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)


class HumanAssignment(Contract):
    expected_request_version: int = Field(ge=1)
    assignee: str | None = Field(default=None, min_length=1, max_length=100)
    due_at: AwareDatetime | None = None


def request_view(row):
    return {
        "id": str(row.id),
        "case_id": str(row.case_id),
        "task_id": str(row.task_id),
        "kind": row.kind,
        "handler": row.handler,
        "subject_id": str(row.subject_id),
        "subject_fingerprint": row.subject_fingerprint,
        "question": row.question,
        "context_summary": row.context_summary,
        "evidence_source_ids": row.evidence_source_ids,
        "allowed_actions": row.allowed_actions,
        "response_schema_version": 1,
        "status": row.status,
        "assignee": row.assignee,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "overdue": bool(row.status == "OPEN" and row.due_at and row.due_at < now()),
        "version": row.version,
        "created_at": row.created_at.isoformat(),
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


async def ensure_request(
    session,
    task,
    *,
    handler,
    subject_id,
    subject_fingerprint,
    question,
    context_summary,
    actions,
    evidence=(),
    kind="REVIEW",
    dedupe_key=None,
):
    rows = (
        await session.scalars(
            select(HumanRequest).where(
                HumanRequest.tenant_id == task.tenant_id,
                HumanRequest.task_id == task.id,
                HumanRequest.handler == handler,
                HumanRequest.status == "OPEN",
            )
        )
    ).all()
    for row in rows:
        if row.subject_id == subject_id and row.subject_fingerprint == subject_fingerprint:
            return row
        row.status, row.resolved_at, row.version = "SUPERSEDED", now(), row.version + 1
    if dedupe_key is None:
        latest = await session.scalar(
            select(HumanDecision.id)
            .join(HumanRequest, HumanRequest.id == HumanDecision.request_id)
            .where(
                HumanRequest.tenant_id == task.tenant_id,
                HumanRequest.task_id == task.id,
                HumanRequest.handler == handler,
            )
            .order_by(HumanDecision.created_at.desc(), HumanDecision.id.desc())
            .limit(1)
        )
        dedupe_key = f"{handler}/{task.id}/{subject_fingerprint}/{latest or 'initial'}"
    existing = await session.scalar(
        select(HumanRequest).where(
            HumanRequest.tenant_id == task.tenant_id, HumanRequest.dedupe_key == dedupe_key
        )
    )
    if existing:
        return existing
    row = HumanRequest(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        kind=kind,
        handler=handler,
        subject_id=subject_id,
        subject_fingerprint=subject_fingerprint,
        question=question,
        context_summary=context_summary,
        evidence_source_ids=list(evidence),
        allowed_actions=actions,
        status="OPEN",
        assignee="local-operator",
        version=1,
        dedupe_key=dedupe_key,
    )
    session.add(row)
    await session.flush()
    session.add(
        ActivityEvent(
            tenant_id=task.tenant_id,
            case_id=task.case_id,
            task_id=task.id,
            event_type="human.request_created",
            producer="human-work",
            dedupe_key=f"created/{row.id}",
            actor_ref="platform",
            payload={"request_id": str(row.id), "question": question, "handler": handler},
        )
    )
    return row


async def retire_requests(session, task, status="SUPERSEDED", *, handler=None):
    query = select(HumanRequest).where(
        HumanRequest.tenant_id == task.tenant_id,
        HumanRequest.task_id == task.id,
        HumanRequest.status == "OPEN",
    )
    if handler:
        query = query.where(HumanRequest.handler == handler)
    for row in (await session.scalars(query)).all():
        row.status, row.resolved_at, row.version = status, now(), row.version + 1


async def record_decision(session, request, *, action, payload, reason, source_id, key, body_hash):
    row = HumanDecision(
        id=uuid4(),
        tenant_id=request.tenant_id,
        case_id=request.case_id,
        request_id=request.id,
        action=action,
        payload=payload,
        reason=reason,
        actor="local-operator",
        reviewed_fingerprint=request.subject_fingerprint,
        source_id=source_id,
        idempotency_key=key,
        request_hash=body_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def request_details(session, row):
    result = request_view(row)
    task = await session.get(Task, row.task_id)
    result["task_version"], result["task_status"] = task.version, task.status
    result["actionable"] = row.status == "OPEN" and (
        task.status == "BLOCKED" or task.task_type == "case.coordination"
    )
    decisions = (
        await session.scalars(
            select(HumanDecision)
            .where(
                HumanDecision.tenant_id == row.tenant_id,
                HumanDecision.request_id == row.id,
            )
            .order_by(HumanDecision.created_at, HumanDecision.id)
        )
    ).all()
    result["decisions"] = [
        {
            "id": str(d.id),
            "action": d.action,
            "payload": d.payload,
            "reason": d.reason,
            "actor": d.actor,
            "created_at": d.created_at.isoformat(),
            "source_id": str(d.source_id),
        }
        for d in decisions
    ]
    continuations = (
        await session.scalars(
            select(HumanContinuation).where(
                HumanContinuation.tenant_id == row.tenant_id,
                HumanContinuation.decision_id.in_([d.id for d in decisions]),
            )
        )
    ).all()
    result["continuations"] = [
        {
            "decision_id": str(c.decision_id),
            "status": c.status,
            "ready_at": c.ready_at.isoformat(),
            "response_due_at": c.response_due_at.isoformat(),
            "source_id": str(c.source_id) if c.source_id else None,
        }
        for c in continuations
    ]
    return result


async def decide_request(session, tenant_id, request_id, body, key):
    from agents_platform.human_request_handlers import apply_response
    from agents_platform.memory_api import wake_task

    async with session.begin():
        await policy_lock(session, tenant_id)
        # Same lock ordering as execution and existing review endpoints: case, task, request.
        identity = (
            await session.execute(
                select(HumanRequest.case_id, HumanRequest.task_id).where(
                    HumanRequest.tenant_id == tenant_id,
                    HumanRequest.id == request_id,
                )
            )
        ).first()
        if not identity:
            raise TaskError(404, "Human request not found")
        case = await session.scalar(
            select(Case)
            .where(
                Case.tenant_id == tenant_id,
                Case.id == identity.case_id,
            )
            .with_for_update()
        )
        task = await session.scalar(
            select(Task)
            .where(
                Task.tenant_id == tenant_id,
                Task.id == identity.task_id,
            )
            .with_for_update()
        )
        row = await session.scalar(
            select(HumanRequest)
            .where(
                HumanRequest.tenant_id == tenant_id,
                HumanRequest.id == request_id,
            )
            .with_for_update()
        )
        body_hash = fingerprint(body.model_dump(mode="json"))
        previous = await session.scalar(
            select(HumanDecision).where(
                HumanDecision.tenant_id == tenant_id,
                HumanDecision.request_id == request_id,
                HumanDecision.idempotency_key == key,
            )
        )
        if previous:
            if previous.request_hash != body_hash:
                raise TaskError(409, "Idempotency-Key used for a different decision")
            return {
                "request": await request_details(session, row),
                "replayed": True,
                "continuation": "Decision saved; inspect task state for processing outcome.",
            }
        stopping_records = (
            row.handler == "records_follow_up.v1" and body.action == "STOP_COLLECTION"
        )
        if (case.status != "OPEN" and not stopping_records) or task.status in (
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        ):
            raise TaskError(409, "Task or case is closed")
        if row.status != "OPEN":
            raise TaskError(409, "This human request is no longer open")
        if (
            row.version != body.expected_request_version
            or task.version != body.expected_task_version
        ):
            raise TaskError(409, "Task or human request changed; refresh before deciding")
        if body.action not in [a["action"] for a in row.allowed_actions]:
            raise TaskError(422, "Action is not permitted for this human request")
        # Client reviews preserve their existing API/source/event semantics and use this ledger.
        if row.handler == "client_report.v1":
            from agents_platform.client_reviews import ReviewDecision, decide_review_in_transaction
            from agents_platform.human_request_handlers import EmptyResponse
            from agents_platform.models import ClientReview

            EmptyResponse.model_validate(body.payload)
            client_review = await session.get(ClientReview, row.subject_id)
            result = await decide_review_in_transaction(
                session,
                tenant_id,
                task.id,
                row.subject_id,
                ReviewDecision(
                    expected_task_version=task.version,
                    expected_review_version=client_review.version,
                    decision=body.action,
                    reason=body.reason,
                ),
                key,
                shared_response=body,
            )
            return {
                "request": await request_details(session, row),
                "replayed": result["replayed"],
                "continuation": "Decision saved; same-task continuation queued.",
            }
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            source_key=f"human-decision/{request_id}/{key}",
            kind="HUMAN_DECISION",
            content={
                "request_id": str(request_id),
                "response": body.model_dump(mode="json"),
                "actor": "local-operator",
                "reviewed_fingerprint": row.subject_fingerprint,
            },
        )
        session.add(source)
        await session.flush()
        decision = await record_decision(
            session,
            row,
            action=body.action,
            payload=body.payload,
            reason=body.reason,
            source_id=source.id,
            key=key,
            body_hash=body_hash,
        )
        await apply_response(session, task, row, decision)
        row.status, row.resolved_at, row.version = "RESOLVED", now(), row.version + 1
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            task_id=task.id,
            event_type="human.decision_recorded",
            producer="human-work",
            dedupe_key=f"decision/{decision.id}",
            actor_ref="local-operator",
            payload={
                "request_id": str(row.id),
                "decision_id": str(decision.id),
                "source_id": str(source.id),
                "action": body.action,
                "reason": body.reason,
            },
        )
        session.add(event)
        await session.flush()
        if task.kind == "AGENT":
            await wake_task(session, task, event)
            if task.status == "CANCELLED":
                session.add(
                    OutboxMessage(
                        tenant_id=tenant_id,
                        event_id=event.id,
                        destination="temporal.signal_task",
                        destination_key=task.workflow_id,
                        payload={"tenant_id": str(tenant_id), "task_id": str(task.id)},
                    )
                )
        else:
            from agents_platform.coordination import enqueue_supervisor

            task.version += 1
            await enqueue_supervisor(session, event, force=True)
        return {
            "request": await request_details(session, row),
            "replayed": False,
            "continuation": "Decision saved; continuation queued. Other blockers still apply.",
        }


router = APIRouter(
    prefix="/v1/firms/{tenant_id}", tags=["human work"], dependencies=[Depends(require_operator)]
)


@router.get("/human-requests")
async def list_requests(
    tenant_id: UUID,
    session: Session,
    task_id: UUID | None = None,
    status: str | None = Query(
        default="OPEN", pattern="^(OPEN|RESOLVED|SUPERSEDED|CANCELLED|ALL)$"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = select(HumanRequest).where(HumanRequest.tenant_id == tenant_id)
    if task_id:
        query = query.where(HumanRequest.task_id == task_id)
    if status and status != "ALL":
        query = query.where(HumanRequest.status == status)
    rows = (
        await session.scalars(
            query.order_by(
                HumanRequest.due_at.asc().nulls_last(), HumanRequest.created_at, HumanRequest.id
            )
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return {
        "items": [await request_details(session, row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/human-requests/{request_id}")
async def get_request(tenant_id: UUID, request_id: UUID, session: Session):
    row = await session.scalar(
        select(HumanRequest).where(
            HumanRequest.tenant_id == tenant_id,
            HumanRequest.id == request_id,
        )
    )
    if not row:
        raise HTTPException(404, "Human request not found")
    return await request_details(session, row)


@router.post("/human-requests/{request_id}/decisions")
async def respond(
    tenant_id: UUID, request_id: UUID, body: HumanResponse, session: Session, idempotency_key: "Key"
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        return await decide_request(session, tenant_id, request_id, body, idempotency_key)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid domain response: " + str(exc)) from exc
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/human-requests/{request_id}/assignment")
async def assign(
    tenant_id: UUID,
    request_id: UUID,
    body: HumanAssignment,
    session: Session,
    idempotency_key: "Key",
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    async with session.begin():
        row = await session.scalar(
            select(HumanRequest)
            .where(
                HumanRequest.tenant_id == tenant_id,
                HumanRequest.id == request_id,
            )
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "Human request not found")
        dedupe = f"assignment/{request_id}/{idempotency_key}"
        existing = await session.scalar(
            select(ActivityEvent).where(
                ActivityEvent.tenant_id == tenant_id,
                ActivityEvent.producer == "human-work",
                ActivityEvent.dedupe_key == dedupe,
            )
        )
        payload = body.model_dump(mode="json")
        if existing:
            if existing.payload["assignment"] != payload:
                raise HTTPException(409, "Idempotency-Key used for a different assignment")
            return {"request": await request_details(session, row), "replayed": True}
        if row.status != "OPEN" or row.version != body.expected_request_version:
            raise HTTPException(409, "Human request changed; refresh before assigning")
        row.assignee, row.due_at, row.version = body.assignee, body.due_at, row.version + 1
        session.add(
            ActivityEvent(
                tenant_id=tenant_id,
                case_id=row.case_id,
                task_id=row.task_id,
                event_type="human.request_assigned",
                producer="human-work",
                dedupe_key=dedupe,
                actor_ref="local-operator",
                payload={"request_id": str(row.id), "assignment": payload},
            )
        )
        return {"request": await request_details(session, row), "replayed": False}


# Import after the contracts: memory_api imports execution through its helpers.
from agents_platform.memory_api import Key  # noqa: E402

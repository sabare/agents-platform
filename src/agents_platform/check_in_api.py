"""One scheduled responsibility, one attributed reply, deterministic comparison."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, Field
from sqlalchemy import select

from agents_platform.client_check_in import report_view
from agents_platform.client_reviews import (
    ReviewDecision,
    acknowledged,
    create_review,
    decide_review,
    review_for_report,
)
from agents_platform.coordination import digest, enqueue_supervisor
from agents_platform.memory import policy_lock
from agents_platform.memory_api import Key, wake_task
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    CaseCoordination,
    ClientCheckIn,
    ClientReport,
    OutboxMessage,
    Source,
    Task,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract
from agents_platform.tasks import TaskError, read_task

router = APIRouter(
    prefix="/v1/firms/{tenant_id}",
    tags=["client check-in"],
    dependencies=[Depends(require_operator)],
)


class CreateCheckIn(Contract):
    case_id: UUID
    client_contact_id: UUID
    parent_task_id: UUID | None = None
    scheduled_at: AwareDatetime
    objective: str = Field(
        default="Check in with the client and report meaningful updates.",
        min_length=1,
        max_length=2000,
    )
    message: str = Field(
        default=(
            "How have you been since our last check-in? Please share changes in how you feel, "
            "daily activities, treatment, or anything you would like the firm to know."
        ),
        min_length=1,
        max_length=2000,
    )


class ClientReply(Contract):
    message: str = Field(min_length=1, max_length=4000)
    pain_score: int | None = Field(default=None, ge=0, le=10)
    daily_function: Literal["UNCHANGED", "BETTER", "WORSE", "NOT_REPORTED"] = "NOT_REPORTED"
    treatment_update: str = Field(default="", max_length=2000)
    treatment_changed: bool = False
    concern: str = Field(default="", max_length=2000)
    requests_help: bool = False


def compare_report(reply, previous):
    old = previous.report if previous else {}
    changes, reasons = {}, []
    for key, value in reply.model_dump().items():
        if key != "message" and old.get(key) != value:
            changes[key] = {"previous": old.get(key), "current": value}
    if previous is None:
        reasons.append("First client report establishes a baseline")
    delta = None
    if reply.pain_score is not None and old.get("pain_score") is not None:
        delta = reply.pain_score - old["pain_score"]
        if abs(delta) >= 2:
            reasons.append(f"Client-reported pain score changed by {delta:+d}")
    if reply.daily_function == "BETTER" and old.get("daily_function") != "BETTER":
        reasons.append("Client reports improved daily function")
    if reply.daily_function == "WORSE":
        reasons.append("Client reports worsening daily function")
    if reply.treatment_changed or (
        reply.treatment_update and reply.treatment_update != old.get("treatment_update")
    ):
        reasons.append("Client reports a treatment update")
    if reply.concern and reply.concern != old.get("concern"):
        reasons.append("Client reports a new or changed concern")
    if reply.requests_help:
        reasons.append("Client explicitly requests help")
    if reply.pain_score is not None and reply.pain_score >= 8:
        reasons.append("Client reports a high pain score; teammate review required")
    review = (
        reply.requests_help
        or bool(reply.concern)
        or reply.daily_function == "WORSE"
        or (delta is not None and delta >= 2)
        or (reply.pain_score is not None and reply.pain_score >= 8)
    )
    if previous and reply.message != old.get("message") and not reasons:
        reasons.append(
            "Client narrative changed without a classified structured update; review needed"
        )
        review = True
    return (
        {
            "policy": "client-change-rules.v1",
            "fields": changes,
            "pain_delta": delta,
            "reasons": reasons,
            "previous_source_id": str(previous.source_id) if previous else None,
            "attribution": "Client self-report, not independently verified",
        },
        bool(reasons),
        review,
    )


async def ensure_monitor(session, tenant_id, case_id):
    coord = await session.scalar(
        select(CaseCoordination).where(
            CaseCoordination.tenant_id == tenant_id, CaseCoordination.case_id == case_id
        )
    )
    if coord:
        return coord
    group = Task(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        kind="GROUP",
        task_type="case.coordination",
        objective="Case coordination",
        input_schema="group.v1",
        input_json={},
        progress_json={},
        completion_json={},
    )
    session.add(group)
    await session.flush()
    coord = CaseCoordination(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        group_task_id=group.id,
        workflow_id=f"firm/{tenant_id}/case/{case_id}/supervisor",
        objective_json={
            "objective": "Monitor client reports and surface meaningful changes for the firm.",
            "targets": [],
            "monitor_client_updates": True,
        },
        plan_json={},
    )
    session.add(coord)
    await session.flush()
    return coord


@router.post("/tasks/client-check-in", status_code=201)
async def create_check_in(tenant_id: UUID, body: CreateCheckIn, session: Session, key: Key):
    async with session.begin():
        await policy_lock(session, tenant_id)
        case = await session.scalar(
            select(Case)
            .where(Case.tenant_id == tenant_id, Case.id == body.case_id)
            .with_for_update(key_share=True)
        )
        if not case:
            raise HTTPException(404, "Case not found")
        creation_key = f"check-in/{key}"
        existing = await session.scalar(
            select(Task).where(Task.tenant_id == tenant_id, Task.creation_key == creation_key)
        )
        fingerprint = digest(body.model_dump(mode="json"))
        if existing:
            if existing.creation_hash != fingerprint:
                raise HTTPException(409, "Idempotency-Key used for another assignment")
            return await read_task(session, tenant_id, existing.id)
        if case.status != "OPEN":
            raise HTTPException(409, "Check-in requires an open case")
        client = await session.scalar(
            select(CaseContact).where(
                CaseContact.tenant_id == tenant_id,
                CaseContact.case_id == case.id,
                CaseContact.contact_id == body.client_contact_id,
            )
        )
        if not client or not {"CLIENT", "PATIENT"}.intersection(client.roles):
            raise HTTPException(422, "Client must belong to this case")
        if body.parent_task_id:
            parent = await session.scalar(
                select(Task).where(
                    Task.tenant_id == tenant_id,
                    Task.case_id == case.id,
                    Task.id == body.parent_task_id,
                    Task.kind == "GROUP",
                    Task.status.not_in(["COMPLETED", "CANCELLED", "FAILED"]),
                )
            )
            if not parent:
                raise HTTPException(422, "Parent must be an open grouping task in this case")
        if not await session.get(AgentVersion, ("client_check_in", 1)) or not await session.get(
            AgentVersion, ("case_supervisor", 1)
        ):
            raise HTTPException(
                409, "Publish check-in and supervisor definitions with the seed command"
            )
        coord = await ensure_monitor(session, tenant_id, case.id)
        task_id, check_in_id, event_id = uuid4(), uuid4(), uuid4()
        task = Task(
            id=task_id,
            tenant_id=tenant_id,
            case_id=case.id,
            parent_task_id=body.parent_task_id or coord.group_task_id,
            kind="AGENT",
            task_type="client.check_in",
            objective=body.objective,
            agent_key="client_check_in",
            agent_version=1,
            input_schema="check_in_assignment.v1",
            input_json={"check_in_id": str(check_in_id)},
            progress_json={},
            completion_json={"schema": "check_in_completion.v1", "requires_attributed_reply": True},
            workflow_id=f"firm/{tenant_id}/task/{task_id}",
            creation_key=creation_key,
            creation_hash=fingerprint,
        )
        session.add(task)
        await session.flush()
        session.add(
            ClientCheckIn(
                id=check_in_id,
                tenant_id=tenant_id,
                case_id=case.id,
                task_id=task_id,
                client_contact_id=body.client_contact_id,
                scheduled_at=body.scheduled_at,
                message=body.message,
            )
        )
        event = ActivityEvent(
            id=event_id,
            tenant_id=tenant_id,
            case_id=case.id,
            task_id=task_id,
            event_type="check_in.scheduled",
            producer="operator",
            dedupe_key=creation_key,
            actor_ref="local-operator",
            payload={
                "scheduled_at": body.scheduled_at.isoformat(),
                "client_contact_id": str(body.client_contact_id),
            },
        )
        session.add(event)
        await session.flush()
        session.add(
            OutboxMessage(
                tenant_id=tenant_id,
                event_id=event_id,
                destination="temporal.start_task",
                destination_key=task.workflow_id,
                payload={"tenant_id": str(tenant_id), "task_id": str(task_id)},
            )
        )
        await enqueue_supervisor(session, event)
        return await read_task(session, tenant_id, task_id)


@router.post("/tasks/{task_id}/client-replies", status_code=201)
async def receive_reply(
    tenant_id: UUID, task_id: UUID, body: ClientReply, session: Session, key: Key
):
    async with session.begin():
        await policy_lock(session, tenant_id)
        # Case serializes reports so concurrent replies use a consistent previous baseline.
        case_id = await session.scalar(
            select(Task.case_id).where(Task.tenant_id == tenant_id, Task.id == task_id)
        )
        if not case_id:
            raise HTTPException(404, "Task not found")
        await session.scalar(
            select(Case)
            .where(Case.tenant_id == tenant_id, Case.id == case_id)
            .with_for_update(key_share=True)
        )
        task = await session.scalar(
            select(Task).where(Task.tenant_id == tenant_id, Task.id == task_id).with_for_update()
        )
        row = await session.scalar(
            select(ClientCheckIn).where(
                ClientCheckIn.tenant_id == tenant_id,
                ClientCheckIn.case_id == case_id,
                ClientCheckIn.task_id == task_id,
            )
        )
        if not row:
            raise HTTPException(422, "Task is not a client check-in")
        source_key = f"client-reply/{task_id}/{key}"
        existing = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        payload = body.model_dump(mode="json")
        if existing:
            if existing.content["report"] != payload:
                raise HTTPException(409, "Idempotency-Key used for a different reply")
            report = await session.scalar(
                select(ClientReport).where(
                    ClientReport.tenant_id == tenant_id, ClientReport.source_id == existing.id
                )
            )
            review = await review_for_report(session, report)
            return {
                "report": report_view(report, review, await acknowledged(session, report, review)),
                "replayed": True,
            }
        if not row.sent_at:
            raise HTTPException(409, "Cannot reply before the simulated check-in was sent")
        if task.status in ("COMPLETED", "CANCELLED", "FAILED"):
            raise HTTPException(409, "Task is closed")
        if await session.scalar(
            select(ClientReport.id).where(
                ClientReport.tenant_id == tenant_id, ClientReport.check_in_id == row.id
            )
        ):
            raise HTTPException(409, "This check-in already has a reply; schedule a new small task")
        previous = await session.scalar(
            select(ClientReport)
            .where(
                ClientReport.tenant_id == tenant_id,
                ClientReport.case_id == case_id,
                ClientReport.client_contact_id == row.client_contact_id,
            )
            .order_by(ClientReport.reported_at.desc(), ClientReport.id.desc())
            .limit(1)
        )
        changes, meaningful, review = compare_report(body, previous)
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            source_key=source_key,
            kind="SIMULATED_CLIENT_SELF_REPORT",
            content={
                "client_contact_id": str(row.client_contact_id),
                "report": payload,
                "simulated": True,
                "attribution": "client self-report; entered by local operator",
            },
        )
        session.add(source)
        await session.flush()
        report = ClientReport(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            check_in_id=row.id,
            client_contact_id=row.client_contact_id,
            source_id=source.id,
            previous_report_id=previous.id if previous else None,
            reported_at=datetime.now(timezone.utc),
            report=payload,
            changes=changes,
            meaningful=meaningful,
            needs_review=review,
        )
        session.add(report)
        await session.flush()
        review = await create_review(session, task, report, source) if review else None
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            task_id=task_id,
            event_type="client.meaningful_update" if meaningful else "client.reply_received",
            producer="simulated-client",
            dedupe_key=source_key,
            actor_ref=f"simulated-client/{row.client_contact_id}",
            payload={
                "source_id": str(source.id),
                "report_id": str(report.id),
                "task_id": str(task_id),
                "client_contact_id": str(row.client_contact_id),
                "meaningful": meaningful,
                "needs_review": bool(review),
                "review_id": str(review.id) if review else None,
                "changes": changes,
                "message": body.message,
            },
        )
        session.add(event)
        await session.flush()
        # Always wake specialist; only material changes go to supervisor.
        if meaningful:
            await wake_task(session, task, event)
        else:
            from agents_platform.models import TaskInbox

            task.version += 1
            session.add(
                TaskInbox(tenant_id=tenant_id, case_id=case_id, task_id=task_id, event_id=event.id)
            )
            session.add(
                OutboxMessage(
                    tenant_id=tenant_id,
                    event_id=event.id,
                    destination="temporal.wake_task",
                    destination_key=task.workflow_id,
                    payload={"tenant_id": str(tenant_id), "task_id": str(task_id)},
                )
            )
        return {"report": report_view(report, review), "replayed": False}


@router.get("/cases/{case_id}/client-reports")
async def client_reports(
    tenant_id: UUID, case_id: UUID, session: Session, client_contact_id: UUID | None = None
):
    if not await session.scalar(
        select(Case.id).where(Case.tenant_id == tenant_id, Case.id == case_id)
    ):
        raise HTTPException(404, "Case not found")
    query = select(ClientReport).where(
        ClientReport.tenant_id == tenant_id, ClientReport.case_id == case_id
    )
    if client_contact_id:
        query = query.where(ClientReport.client_contact_id == client_contact_id)
    rows = (
        await session.scalars(
            query.order_by(ClientReport.reported_at.desc(), ClientReport.id.desc()).limit(100)
        )
    ).all()
    result = []
    for report in rows:
        review = await review_for_report(session, report)
        result.append(report_view(report, review, await acknowledged(session, report, review)))
    return result


@router.post("/tasks/{task_id}/client-reviews/{review_id}/decisions")
async def decide_client_review(
    tenant_id: UUID,
    task_id: UUID,
    review_id: UUID,
    body: ReviewDecision,
    session: Session,
    key: Key,
):
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        return await decide_review(session, tenant_id, task_id, review_id, body, key.strip())
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

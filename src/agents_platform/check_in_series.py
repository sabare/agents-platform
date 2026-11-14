"""Bounded fixed-delay series. Each occurrence reuses the ordinary check-in task."""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import exists, or_, select
from temporalio import activity

from agents_platform.check_in_api import (
    CreateCheckIn,
    create_check_in_in_transaction,
    ensure_monitor,
)
from agents_platform.coordination import digest
from agents_platform.memory import policy_lock
from agents_platform.memory_api import Key, wake_task
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    CheckInSeries,
    ClientCheckIn,
    ClientReview,
    OutboxMessage,
    Source,
    Task,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract
from agents_platform.telemetry import observed_activity

router = APIRouter(
    prefix="/v1/firms/{tenant_id}/cases/{case_id}/check-in-series",
    tags=["check-in series"],
    dependencies=[Depends(require_operator)],
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def now():
    return datetime.now(timezone.utc)


class CreateSeries(Contract):
    client_contact_id: UUID
    interval_seconds: int = Field(ge=60, le=31536000)
    first_due_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    max_occurrences: int | None = Field(default=None, ge=1, le=100)
    objective: str = Field(
        default="Periodically check in with the client.", min_length=1, max_length=2000
    )
    message: str = Field(
        default="How have you been? Please share changes or anything the firm should know.",
        min_length=1,
        max_length=2000,
    )

    @model_validator(mode="after")
    def bounded(self):
        if self.ends_at is None and self.max_occurrences is None:
            raise ValueError("Provide an end date or maximum number of check-ins")
        if self.ends_at and self.ends_at <= self.first_due_at:
            raise ValueError("End date must be after the first due date")
        return self


class SeriesControl(Contract):
    expected_version: int = Field(ge=1)
    action: Literal["PAUSE", "RESUME", "STOP"]
    reason: str = Field(min_length=1, max_length=2000)


def series_view(row):
    return {
        "id": str(row.id),
        "case_id": str(row.case_id),
        "client_contact_id": str(row.client_contact_id),
        "group_task_id": str(row.group_task_id),
        "active_task_id": str(row.active_task_id) if row.active_task_id else None,
        "workflow_id": row.workflow_id,
        "status": row.status,
        "version": row.version,
        "interval_seconds": row.interval_seconds,
        "next_due_at": row.next_due_at.isoformat() if row.next_due_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "max_occurrences": row.max_occurrences,
        "occurrence_count": row.occurrence_count,
        "objective": row.objective,
        "message": row.message,
        "blocked_reason": row.blocked_reason,
        "schedule": "Fixed delay after completion; no catch-up burst; hard cap 100 occurrences",
    }


async def enqueue_series(session, row, event):
    await session.flush()
    exists = await session.scalar(
        select(OutboxMessage.id).where(
            OutboxMessage.tenant_id == row.tenant_id,
            OutboxMessage.event_id == event.id,
            OutboxMessage.destination == "temporal.wake_series",
            OutboxMessage.destination_key == row.workflow_id,
        )
    )
    if not exists:
        session.add(
            OutboxMessage(
                tenant_id=row.tenant_id,
                event_id=event.id,
                destination="temporal.wake_series",
                destination_key=row.workflow_id,
                payload={
                    "tenant_id": str(row.tenant_id),
                    "case_id": str(row.case_id),
                    "series_id": str(row.id),
                },
            )
        )


async def notify_series(session, task, event):
    if task.task_type != "client.check_in":
        return
    checkin = await session.scalar(
        select(ClientCheckIn).where(
            ClientCheckIn.tenant_id == task.tenant_id, ClientCheckIn.task_id == task.id
        )
    )
    if not checkin:
        return
    from agents_platform.monthly_check_ins import notify_monthly

    await notify_monthly(session, task, event)
    rows = await session.scalars(
        select(CheckInSeries).where(
            CheckInSeries.tenant_id == task.tenant_id,
            CheckInSeries.case_id == task.case_id,
            CheckInSeries.client_contact_id == checkin.client_contact_id,
            CheckInSeries.status.in_(["ACTIVE", "PAUSED"]),
        )
    )
    for row in rows:
        await enqueue_series(session, row, event)


async def series_event(session, row, event_type, payload, *, actor="platform"):
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        task_id=row.group_task_id,
        event_type=event_type,
        producer="check-in-series",
        dedupe_key=str(uuid4()),
        actor_ref=actor,
        payload={"series_id": str(row.id), **payload},
    )
    session.add(event)
    await session.flush()
    return event


async def cancel_unsent(session, row, event):
    if not row.active_task_id:
        return
    task = await session.scalar(
        select(Task)
        .where(Task.tenant_id == row.tenant_id, Task.id == row.active_task_id)
        .with_for_update()
    )
    checkin = await session.scalar(
        select(ClientCheckIn).where(
            ClientCheckIn.tenant_id == row.tenant_id, ClientCheckIn.task_id == task.id
        )
    )
    if checkin and checkin.sent_at is None and task.status not in TERMINAL:
        task.status, task.version = "CANCELLED", task.version + 1
        task.progress_json = {
            **task.progress_json,
            "last_reason": "Recurring series stopped or ended before send.",
        }
        session.add(
            OutboxMessage(
                tenant_id=row.tenant_id,
                event_id=event.id,
                destination="temporal.signal_task",
                destination_key=task.workflow_id,
                payload={"tenant_id": str(row.tenant_id), "task_id": str(task.id)},
            )
        )


@router.post("", status_code=201)
async def create_series(
    tenant_id: UUID, case_id: UUID, body: CreateSeries, session: Session, key: Key
):
    async with session.begin():
        return await create_series_in_transaction(session, tenant_id, case_id, body, key)


async def create_series_in_transaction(session, tenant_id, case_id, body, key):
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    await policy_lock(session, tenant_id, publishing=True)
    case = await session.scalar(
        select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
    )
    if not case:
        raise HTTPException(404, "Case not found")
    previous = await session.scalar(
        select(CheckInSeries).where(
            CheckInSeries.tenant_id == tenant_id, CheckInSeries.creation_key == key
        )
    )
    fingerprint = digest({"case_id": str(case_id), **body.model_dump(mode="json")})
    if previous:
        if previous.creation_hash != fingerprint:
            raise HTTPException(409, "Idempotency key conflicts with another series")
        return {**series_view(previous), "replayed": True}
    if case.status != "OPEN":
        raise HTTPException(409, "Case is closed")
    if body.ends_at and body.ends_at <= now():
        raise HTTPException(422, "End date is in the past")
    contact = await session.scalar(
        select(CaseContact).where(
            CaseContact.tenant_id == tenant_id,
            CaseContact.case_id == case_id,
            CaseContact.contact_id == body.client_contact_id,
        )
    )
    if not contact or not {"CLIENT", "PATIENT"}.intersection(contact.roles):
        raise HTTPException(422, "Client must belong to this case")
    if await session.scalar(
        select(CheckInSeries.id).where(
            CheckInSeries.tenant_id == tenant_id,
            CheckInSeries.case_id == case_id,
            CheckInSeries.client_contact_id == body.client_contact_id,
            CheckInSeries.status.in_(["ACTIVE", "PAUSED"]),
        )
    ):
        raise HTTPException(409, "Client already has a live series in this case")
    from agents_platform.check_in_contact import contact_policy
    from agents_platform.models import MonthlyCheckInSchedule

    if await session.scalar(
        select(MonthlyCheckInSchedule.id).where(
            MonthlyCheckInSchedule.tenant_id == tenant_id,
            MonthlyCheckInSchedule.case_id == case_id,
            MonthlyCheckInSchedule.client_contact_id == body.client_contact_id,
            MonthlyCheckInSchedule.status.in_(["ACTIVE", "PAUSED"]),
        )
    ):
        raise HTTPException(
            409, "Client already has a live monthly schedule; stop it before another cadence"
        )
    policy = await contact_policy(session, tenant_id, case_id, body.client_contact_id)
    if policy and body.interval_seconds < policy.minimum_gap_days * 86400:
        raise HTTPException(
            409, "Series interval conflicts with this client's contact-spacing policy"
        )
    if not await session.get(AgentVersion, ("client_check_in", 1)):
        raise HTTPException(409, "Publish check-in definitions with the seed command")
    coord = await ensure_monitor(session, tenant_id, case_id)
    series_id, group_id = uuid4(), uuid4()
    session.add(
        Task(
            id=group_id,
            tenant_id=tenant_id,
            case_id=case_id,
            parent_task_id=coord.group_task_id,
            kind="GROUP",
            task_type="client.check_in_series",
            objective=body.objective,
            input_schema="group.v1",
            input_json={"series_id": str(series_id)},
            progress_json={},
            completion_json={},
        )
    )
    await session.flush()
    row = CheckInSeries(
        id=series_id,
        tenant_id=tenant_id,
        case_id=case_id,
        client_contact_id=body.client_contact_id,
        group_task_id=group_id,
        workflow_id=f"firm/{tenant_id}/series/{series_id}",
        interval_seconds=body.interval_seconds,
        next_due_at=body.first_due_at,
        ends_at=body.ends_at,
        max_occurrences=body.max_occurrences,
        objective=body.objective,
        message=body.message,
        creation_key=key,
        creation_hash=fingerprint,
        status="ACTIVE",
        version=1,
        occurrence_count=0,
    )
    session.add(row)
    await session.flush()
    event = await series_event(
        session, row, "series.created", body.model_dump(mode="json"), actor="local-operator"
    )
    await enqueue_series(session, row, event)
    return {**series_view(row), "replayed": False}


@router.get("")
async def list_series(tenant_id: UUID, case_id: UUID, session: Session):
    if not await session.scalar(
        select(Case.id).where(Case.tenant_id == tenant_id, Case.id == case_id)
    ):
        raise HTTPException(404, "Case not found")
    return [
        series_view(r)
        for r in await session.scalars(
            select(CheckInSeries)
            .where(CheckInSeries.tenant_id == tenant_id, CheckInSeries.case_id == case_id)
            .order_by(CheckInSeries.created_at.desc())
            .limit(100)
        )
    ]


@router.post("/{series_id}/control")
async def control_series(
    tenant_id: UUID, case_id: UUID, series_id: UUID, body: SeriesControl, session: Session, key: Key
):
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    async with session.begin():
        # Exclusive partner of the action-time shared lock: no pause/send race.
        await policy_lock(session, tenant_id, publishing=True)
        case = await session.scalar(
            select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
        )
        row = await session.scalar(
            select(CheckInSeries)
            .where(
                CheckInSeries.tenant_id == tenant_id,
                CheckInSeries.case_id == case_id,
                CheckInSeries.id == series_id,
            )
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "Series not found")
        source_key = f"series-control/{series_id}/{key}"
        existing = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        payload = body.model_dump(mode="json")
        if existing:
            if existing.content != payload:
                raise HTTPException(409, "Idempotency key conflicts")
            return {**series_view(row), "replayed": True}
        if row.version != body.expected_version:
            raise HTTPException(409, "Series changed; refresh")
        if row.status in ("STOPPED", "FINISHED"):
            raise HTTPException(409, "Series has ended")
        if body.action == "RESUME" and (
            row.status != "PAUSED" or (row.ends_at and row.ends_at <= now())
        ):
            raise HTTPException(409, "Only an unexpired paused series can resume")
        if body.action == "PAUSE" and row.status != "ACTIVE":
            raise HTTPException(409, "Series is not active")
        if body.action == "RESUME":
            if case is None or case.status != "OPEN":
                raise HTTPException(409, "Cannot resume a closed case")
            prior = await session.get(Task, row.active_task_id) if row.active_task_id else None
            if prior and prior.status in ("FAILED", "CANCELLED"):
                raise HTTPException(
                    409, "Previous occurrence failed; stop the series and resolve that work"
                )
        row.status = {"PAUSE": "PAUSED", "RESUME": "ACTIVE", "STOP": "STOPPED"}[body.action]
        row.version += 1
        if body.action == "STOP":
            row.next_due_at = None
        row.blocked_reason = body.reason if body.action != "RESUME" else None
        group = await session.get(Task, row.group_task_id)
        group.status = {"PAUSE": "BLOCKED", "RESUME": "WAITING", "STOP": "CANCELLED"}[body.action]
        group.version += 1
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            source_key=source_key,
            kind="SERIES_CONTROL",
            content=payload,
        )
        session.add(source)
        event = await series_event(
            session, row, "series.controlled", {**payload, "source_id": str(source.id)}
        )
        if body.action == "STOP":
            await cancel_unsent(session, row, event)
        elif row.active_task_id:
            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == tenant_id, Task.id == row.active_task_id)
                .with_for_update()
            )
            if task.status not in TERMINAL:
                await wake_task(session, task, event)
        await enqueue_series(session, row, event)
        return {**series_view(row), "replayed": False}


class SeriesActivities:
    def __init__(self, database):
        self.database = database

    @activity.defn
    @observed_activity
    async def advance_series(self, activation):
        tenant, series_id = UUID(activation["tenant_id"]), UUID(activation["series_id"])
        async with self.database.sessions.begin() as session:
            await policy_lock(session, tenant, publishing=True)
            case = await session.scalar(
                select(Case)
                .where(Case.tenant_id == tenant, Case.id == UUID(activation["case_id"]))
                .with_for_update()
            )
            if case is None:
                return {"status": "CLOSED"}
            row = await session.scalar(
                select(CheckInSeries)
                .where(
                    CheckInSeries.tenant_id == tenant,
                    CheckInSeries.case_id == case.id,
                    CheckInSeries.id == series_id,
                )
                .with_for_update()
            )
            if not row:
                return {"status": "CLOSED"}
            if row.status in ("STOPPED", "FINISHED"):
                return {"status": "CLOSED"}
            group = await session.get(Task, row.group_task_id)
            if case.status != "OPEN" or (row.ends_at and row.ends_at <= now()):
                row.status = "FINISHED" if case.status == "OPEN" else "STOPPED"
                row.version += 1
                row.next_due_at = None
                group.status = "COMPLETED" if row.status == "FINISHED" else "CANCELLED"
                group.version += 1
                event = await series_event(
                    session, row, "series.ended", {"reason": "End date reached or case closed"}
                )
                await cancel_unsent(session, row, event)
                return {"status": "CLOSED"}
            waiting = {"status": "WAITING"}
            if row.ends_at:
                waiting["wake_at"] = row.ends_at.isoformat()
            if row.status == "PAUSED":
                return waiting
            if row.active_task_id:
                task = await session.scalar(
                    select(Task).where(Task.tenant_id == tenant, Task.id == row.active_task_id)
                )
                if task.status == "COMPLETED":
                    row.active_task_id = None
                    row.next_due_at = now() + timedelta(seconds=row.interval_seconds)
                    row.blocked_reason = None
                    row.version += 1
                elif task.status in ("FAILED", "CANCELLED"):
                    row.status = "PAUSED"
                    row.version += 1
                    row.blocked_reason = (
                        "Previous occurrence failed or was cancelled; "
                        "stop this series or resolve that work before continuing."
                    )
                    group.status = "BLOCKED"
                    group.version += 1
                    return waiting
                else:
                    row.blocked_reason = (
                        "Previous check-in is awaiting reply, review or completion."
                    )
                    return waiting
            if row.occurrence_count >= min(row.max_occurrences or 100, 100):
                row.status = "FINISHED"
                row.next_due_at = None
                row.version += 1
                group.status = "COMPLETED"
                group.version += 1
                await series_event(
                    session, row, "series.ended", {"reason": "Occurrence limit reached"}
                )
                return {"status": "CLOSED"}
            # Other manually assigned/sent work for this client must finish too.
            overlap = await session.scalar(
                select(Task.id)
                .join(
                    ClientCheckIn,
                    (ClientCheckIn.task_id == Task.id)
                    & (ClientCheckIn.tenant_id == Task.tenant_id),
                )
                .where(
                    Task.tenant_id == tenant,
                    Task.case_id == row.case_id,
                    ClientCheckIn.client_contact_id == row.client_contact_id,
                    or_(
                        Task.status.not_in(TERMINAL),
                        exists(
                            select(ClientReview.id).where(
                                ClientReview.tenant_id == tenant,
                                ClientReview.case_id == row.case_id,
                                ClientReview.task_id == Task.id,
                                ClientReview.status == "OPEN",
                            )
                        ),
                    ),
                )
                .limit(1)
            )
            if overlap:
                row.blocked_reason = "Another check-in for this client is still open."
                return waiting
            if row.next_due_at and row.next_due_at > now():
                waiting["wake_at"] = min(
                    row.next_due_at, row.ends_at or row.next_due_at
                ).isoformat()
                return waiting
            from agents_platform.check_in_contact import assess_contact

            assessment = await assess_contact(session, tenant, row.case_id, row.client_contact_id)
            if assessment["code"] == "RECENT_CONTACT":
                row.next_due_at = datetime.fromisoformat(assessment["eligible_after"])
                row.blocked_reason = assessment["reason"]
                row.version += 1
                waiting["wake_at"] = min(
                    row.next_due_at, row.ends_at or row.next_due_at
                ).isoformat()
                return waiting
            occurrence = row.occurrence_count + 1
            child = await create_check_in_in_transaction(
                session,
                tenant,
                CreateCheckIn(
                    case_id=row.case_id,
                    client_contact_id=row.client_contact_id,
                    parent_task_id=row.group_task_id,
                    scheduled_at=now(),
                    objective=f"{row.objective} / check-in {occurrence}",
                    message=row.message,
                ),
                f"series/{row.id}/{occurrence}",
                series=row,
                occurrence=occurrence,
            )
            row.active_task_id = child.id
            row.occurrence_count = occurrence
            row.next_due_at = None
            row.version += 1
            row.blocked_reason = "Awaiting this occurrence's completion."
            group.status = "WAITING"
            group.version += 1
            await series_event(
                session,
                row,
                "series.occurrence_created",
                {"task_id": str(child.id), "occurrence": occurrence},
            )
            return waiting

    @activity.defn
    @observed_activity
    async def fail_series(self, activation):
        async with self.database.sessions.begin() as session:
            await policy_lock(session, UUID(activation["tenant_id"]), publishing=True)
            row = await session.scalar(
                select(CheckInSeries)
                .where(
                    CheckInSeries.tenant_id == UUID(activation["tenant_id"]),
                    CheckInSeries.id == UUID(activation["series_id"]),
                )
                .with_for_update()
            )
            if row and row.status == "ACTIVE":
                row.status = "PAUSED"
                row.version += 1
                row.blocked_reason = "Series activity retries exhausted; inspect before resuming."
                group = await session.get(Task, row.group_task_id)
                group.status = "BLOCKED"
                group.version += 1
                await series_event(session, row, "series.failed", {"reason": row.blocked_reason})


async def check_in_gate(session, checkin):
    if not checkin.series_id:
        return None
    row = await session.scalar(
        select(CheckInSeries).where(
            CheckInSeries.tenant_id == checkin.tenant_id,
            CheckInSeries.case_id == checkin.case_id,
            CheckInSeries.id == checkin.series_id,
        )
    )
    reason = None
    if not row or row.status != "ACTIVE":
        reason = "Recurring check-in series is paused or stopped; do not send."
    elif row.ends_at and row.ends_at <= now():
        reason = "Recurring check-in series end date reached; do not send."
    return {
        "id": str(checkin.series_id),
        "status": row.status if row else "STOPPED",
        "suspension_reason": reason if checkin.sent_at is None else None,
    }

"""Calendar timers wake case coordination; approved occurrences become specialist tasks."""

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select
from temporalio import activity

from agents_platform.check_in_contact import assess_contact
from agents_platform.memory import memory_for_task, policy_lock, stop_contact
from agents_platform.memory_api import Key, wake_task
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    CaseCoordination,
    CheckInSeries,
    ClientCheckIn,
    MonthlyCheckInOccurrence,
    MonthlyCheckInSchedule,
    OutboxMessage,
    Source,
    Task,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract
from agents_platform.telemetry import observed_activity

router = APIRouter(
    prefix="/v1/firms/{tenant_id}/cases/{case_id}/monthly-check-ins",
    tags=["monthly check-ins"],
    dependencies=[Depends(require_operator)],
)
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def now():
    return datetime.now(timezone.utc)


def calendar_due(year, month, day, local_time, zone_name):
    zone = ZoneInfo(zone_name)
    hour, minute = map(int, local_time.split(":"))
    wall = datetime(year, month, min(day, calendar.monthrange(year, month)[1]), hour, minute)
    # Choose the first repeated wall time; nonexistent times shift forward by the DST gap.
    return wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)


def following_month(due_at, row):
    local = due_at.astimezone(ZoneInfo(row.timezone))
    year, month = local.year + (local.month == 12), local.month % 12 + 1
    return calendar_due(year, month, row.day_of_month, row.local_time, row.timezone)


def first_due(row, at, starts_on=None):
    local = at.astimezone(ZoneInfo(row.timezone))
    start = max(local.date(), starts_on or local.date())
    candidate = calendar_due(
        start.year, start.month, row.day_of_month, row.local_time, row.timezone
    )
    if candidate < at or candidate.astimezone(ZoneInfo(row.timezone)).date() < start:
        candidate = following_month(candidate, row)
    return candidate


class CreateMonthly(Contract):
    client_contact_id: UUID
    timezone: str = Field(min_length=1, max_length=100)
    day_of_month: int = Field(ge=1, le=31)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    starts_on: date | None = None
    minimum_gap_days: int = Field(default=28, ge=1, le=365)
    max_occurrences: int = Field(default=12, ge=1, le=120)
    ends_at: datetime | None = None
    objective: str = Field(
        default="Check in with the client monthly and report meaningful changes.",
        min_length=1,
        max_length=2000,
    )
    message: str = Field(
        default=(
            "How have you been? Please share changes in how you feel, treatment, "
            "daily activities, or anything the firm should know."
        ),
        min_length=1,
        max_length=2000,
    )

    @model_validator(mode="after")
    def validate_schedule(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone, such as Asia/Kolkata") from exc
        if self.ends_at and self.ends_at.tzinfo is None:
            raise ValueError("ends_at requires a timezone")
        if self.starts_on and self.starts_on.year > 9980:
            raise ValueError("Start year is outside the supported calendar range")
        if not self.objective.strip() or not self.message.strip():
            raise ValueError("Objective and message must not be blank")
        return self


class MonthlyControl(Contract):
    expected_version: int = Field(ge=1)
    action: Literal["PAUSE", "RESUME", "STOP"]
    reason: str = Field(min_length=1, max_length=2000)


def schedule_view(row):
    fields = [
        "id",
        "case_id",
        "client_contact_id",
        "group_task_id",
        "status",
        "version",
        "timezone",
        "day_of_month",
        "local_time",
        "minimum_gap_days",
        "max_occurrences",
        "occurrence_count",
        "objective",
        "message",
        "workflow_id",
    ]
    result = {
        f: str(getattr(row, f)) if isinstance(getattr(row, f), UUID) else getattr(row, f)
        for f in fields
    }
    result.update(
        next_due_at=row.next_due_at.isoformat() if row.next_due_at else None,
        ends_at=row.ends_at.isoformat() if row.ends_at else None,
        dispatch_window_hours=24,
        spacing_policy=(
            "Local calendar days after the latest sent check-in or client reply; "
            "manual tasks count."
        ),
    )
    return result


def occurrence_view(row):
    return {
        "id": str(row.id),
        "month": row.month_key,
        "status": row.status,
        "due_at": row.due_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "task_id": str(row.task_id) if row.task_id else None,
        "covering_task_id": str(row.covering_task_id) if row.covering_task_id else None,
        "reason": row.reason,
        "decision_source_id": str(row.decision_source_id) if row.decision_source_id else None,
    }


async def schedule_event(session, row, kind, payload, *, actor="platform/monthly"):
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        task_id=row.group_task_id,
        event_type=kind,
        producer="monthly-check-in",
        actor_ref=actor,
        dedupe_key=str(uuid4()),
        payload={"schedule_id": str(row.id), **payload},
    )
    session.add(event)
    await session.flush()
    return event


async def wake_schedule(session, row, event):
    session.add(
        OutboxMessage(
            tenant_id=row.tenant_id,
            event_id=event.id,
            destination="temporal.wake_monthly",
            destination_key=row.workflow_id,
            payload={
                "tenant_id": str(row.tenant_id),
                "case_id": str(row.case_id),
                "schedule_id": str(row.id),
            },
        )
    )


async def read_schedule(session, row):
    occurrences = (
        await session.scalars(
            select(MonthlyCheckInOccurrence)
            .where(
                MonthlyCheckInOccurrence.tenant_id == row.tenant_id,
                MonthlyCheckInOccurrence.schedule_id == row.id,
            )
            .order_by(MonthlyCheckInOccurrence.due_at.desc())
            .limit(120)
        )
    ).all()
    return {**schedule_view(row), "occurrences": [occurrence_view(i) for i in occurrences]}


@router.post("", status_code=201)
async def create_monthly(
    tenant_id: UUID, case_id: UUID, body: CreateMonthly, session: Session, key: Key
):
    async with session.begin():
        return await create_monthly_in_transaction(session, tenant_id, case_id, body, key)


async def create_monthly_in_transaction(session, tenant_id, case_id, body, key):
    from agents_platform.check_in_api import ensure_monitor
    from agents_platform.coordination import digest

    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    await policy_lock(session, tenant_id, publishing=True)
    case = await session.scalar(
        select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
    )
    if not case:
        raise HTTPException(404, "Case not found")
    previous = await session.scalar(
        select(MonthlyCheckInSchedule).where(
            MonthlyCheckInSchedule.tenant_id == tenant_id,
            MonthlyCheckInSchedule.creation_key == key,
        )
    )
    fingerprint = digest({"case_id": str(case_id), **body.model_dump(mode="json")})
    if previous:
        if previous.creation_hash != fingerprint:
            raise HTTPException(409, "Idempotency key conflicts with another schedule")
        return {**await read_schedule(session, previous), "replayed": True}
    if case.status != "OPEN":
        raise HTTPException(409, "Monthly check-ins require an open case")
    contact = await session.scalar(
        select(CaseContact).where(
            CaseContact.tenant_id == tenant_id,
            CaseContact.case_id == case_id,
            CaseContact.contact_id == body.client_contact_id,
        )
    )
    if not contact or not {"CLIENT", "PATIENT"}.intersection(contact.roles):
        raise HTTPException(422, "Client must belong to this case")
    for model in (MonthlyCheckInSchedule, CheckInSeries):
        if await session.scalar(
            select(model.id).where(
                model.tenant_id == tenant_id,
                model.case_id == case_id,
                model.client_contact_id == body.client_contact_id,
                model.status.in_(["ACTIVE", "PAUSED"]),
            )
        ):
            raise HTTPException(
                409, "Client already has a live cadence; stop it before creating another"
            )
    if not await session.get(AgentVersion, ("client_check_in", 1)) or not (
        await session.get(AgentVersion, ("case_supervisor", 2))
        or await session.get(AgentVersion, ("case_supervisor", 1))
    ):
        raise HTTPException(
            409, "Publish check-in and supervisor definitions with the seed command"
        )
    coord = await ensure_monitor(session, tenant_id, case_id)
    schedule_id, group_id = uuid4(), uuid4()
    session.add(
        Task(
            id=group_id,
            tenant_id=tenant_id,
            case_id=case_id,
            parent_task_id=coord.group_task_id,
            kind="GROUP",
            task_type="client.monthly_schedule",
            objective=body.objective,
            input_schema="group.v1",
            input_json={"monthly_schedule_id": str(schedule_id)},
            progress_json={},
            completion_json={},
        )
    )
    await session.flush()
    row = MonthlyCheckInSchedule(
        id=schedule_id,
        tenant_id=tenant_id,
        case_id=case_id,
        client_contact_id=body.client_contact_id,
        group_task_id=group_id,
        workflow_id=f"firm/{tenant_id}/monthly/{schedule_id}",
        status="ACTIVE",
        version=1,
        timezone=body.timezone,
        day_of_month=body.day_of_month,
        local_time=body.local_time,
        minimum_gap_days=body.minimum_gap_days,
        dispatch_after=now(),
        ends_at=body.ends_at,
        max_occurrences=body.max_occurrences,
        occurrence_count=0,
        objective=body.objective,
        message=body.message,
        creation_key=key,
        creation_hash=fingerprint,
    )
    row.next_due_at = first_due(row, now(), body.starts_on)
    if row.ends_at and row.ends_at <= row.next_due_at:
        raise HTTPException(422, "End time must follow the first scheduled check-in")
    session.add(row)
    await session.flush()
    event = await schedule_event(
        session, row, "monthly.created", body.model_dump(mode="json"), actor="local-operator"
    )
    await wake_schedule(session, row, event)
    return {**await read_schedule(session, row), "replayed": False}


@router.get("")
async def list_monthly(tenant_id: UUID, case_id: UUID, session: Session):
    if not await session.scalar(
        select(Case.id).where(Case.tenant_id == tenant_id, Case.id == case_id)
    ):
        raise HTTPException(404, "Case not found")
    rows = (
        await session.scalars(
            select(MonthlyCheckInSchedule)
            .where(
                MonthlyCheckInSchedule.tenant_id == tenant_id,
                MonthlyCheckInSchedule.case_id == case_id,
            )
            .order_by(MonthlyCheckInSchedule.created_at.desc())
            .limit(100)
        )
    ).all()
    return [await read_schedule(session, r) for r in rows]


@router.get("/{schedule_id}")
async def get_monthly(tenant_id: UUID, case_id: UUID, schedule_id: UUID, session: Session):
    row = await session.scalar(
        select(MonthlyCheckInSchedule).where(
            MonthlyCheckInSchedule.tenant_id == tenant_id,
            MonthlyCheckInSchedule.case_id == case_id,
            MonthlyCheckInSchedule.id == schedule_id,
        )
    )
    if not row:
        raise HTTPException(404, "Schedule not found")
    return await read_schedule(session, row)


async def cancel_unsent(session, row, reason):
    occurrences = (
        await session.scalars(
            select(MonthlyCheckInOccurrence).where(
                MonthlyCheckInOccurrence.tenant_id == row.tenant_id,
                MonthlyCheckInOccurrence.schedule_id == row.id,
                MonthlyCheckInOccurrence.status.in_(["DUE", "BLOCKED", "DELEGATED"]),
            )
        )
    ).all()
    for occ in occurrences:
        checkin = await session.scalar(
            select(ClientCheckIn).where(
                ClientCheckIn.tenant_id == row.tenant_id,
                ClientCheckIn.monthly_occurrence_id == occ.id,
            )
        )
        if checkin and checkin.sent_at:
            continue
        if occ.task_id:
            task = await session.get(Task, occ.task_id, with_for_update=True)
            if task.status not in TERMINAL:
                task.status = "CANCELLED"
                task.progress_json = {**task.progress_json, "last_reason": reason}
                event = await schedule_event(
                    session,
                    row,
                    "monthly.cancelled_unsent",
                    {"task_id": str(task.id), "reason": reason},
                )
                await wake_task(session, task, event)
        occ.status, occ.reason = "EXPIRED", reason


@router.post("/{schedule_id}/control")
async def control_monthly(
    tenant_id: UUID,
    case_id: UUID,
    schedule_id: UUID,
    body: MonthlyControl,
    session: Session,
    key: Key,
):
    from agents_platform.coordination import enqueue_supervisor

    if not key.strip() or not body.reason.strip():
        raise HTTPException(422, "Key and reason must not be blank")
    async with session.begin():
        await policy_lock(session, tenant_id, publishing=True)
        case = await session.scalar(
            select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
        )
        row = await session.scalar(
            select(MonthlyCheckInSchedule)
            .where(
                MonthlyCheckInSchedule.tenant_id == tenant_id,
                MonthlyCheckInSchedule.case_id == case_id,
                MonthlyCheckInSchedule.id == schedule_id,
            )
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "Schedule not found")
        source_key = f"monthly-control/{row.id}/{key}"
        previous = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        payload = body.model_dump(mode="json")
        if previous:
            if previous.content != payload:
                raise HTTPException(409, "Idempotency key conflicts")
            return {**await read_schedule(session, row), "replayed": True}
        if row.version != body.expected_version or row.status in ("STOPPED", "FINISHED"):
            raise HTTPException(409, "Schedule changed or ended; refresh")
        if body.action == "RESUME" and (
            row.status != "PAUSED"
            or not case
            or case.status != "OPEN"
            or (row.ends_at and row.ends_at <= now())
        ):
            raise HTTPException(409, "Only an unexpired paused schedule in an open case can resume")
        if body.action == "PAUSE" and row.status != "ACTIVE":
            raise HTTPException(409, "Schedule is not active")
        row.status = {"PAUSE": "PAUSED", "RESUME": "ACTIVE", "STOP": "STOPPED"}[body.action]
        row.version += 1
        if body.action == "RESUME":
            row.dispatch_after = now()
        if body.action == "STOP":
            row.next_due_at = None
            await cancel_unsent(session, row, body.reason)
        group = await session.get(Task, row.group_task_id)
        group.status = (
            "CANCELLED"
            if body.action == "STOP"
            else "BLOCKED"
            if body.action == "PAUSE"
            else "WAITING"
        )
        group.version += 1
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            source_key=source_key,
            kind="MONTHLY_SCHEDULE_CONTROL",
            content=payload,
        )
        session.add(source)
        await session.flush()
        event = await schedule_event(
            session,
            row,
            "monthly.controlled",
            {**payload, "source_id": str(source.id)},
            actor="local-operator",
        )
        children = (
            await session.scalars(
                select(Task)
                .join(ClientCheckIn, ClientCheckIn.task_id == Task.id)
                .join(
                    MonthlyCheckInOccurrence,
                    MonthlyCheckInOccurrence.id == ClientCheckIn.monthly_occurrence_id,
                )
                .where(
                    Task.tenant_id == tenant_id,
                    MonthlyCheckInOccurrence.schedule_id == row.id,
                    Task.status.not_in(TERMINAL),
                )
                .with_for_update(of=Task)
            )
        ).all()
        for task in children:
            await wake_task(session, task, event)
        await enqueue_supervisor(session, event, force=True)
        await wake_schedule(session, row, event)
        return {**await read_schedule(session, row), "replayed": False}


async def record_decision(
    session, row, occ, status, reason, *, contact=None, memory=None, covering=None
):
    if occ.status == status and occ.reason == reason:
        return
    occ.status, occ.reason, occ.covering_task_id = (
        status,
        reason,
        UUID(covering) if covering else None,
    )
    occ.decision_revision += 1
    source = Source(
        id=uuid4(),
        tenant_id=row.tenant_id,
        case_id=row.case_id,
        source_key=f"monthly-decision/{occ.id}/{occ.decision_revision}",
        kind="MONTHLY_SUPERVISOR_DECISION",
        content={
            "occurrence_id": str(occ.id),
            "month": occ.month_key,
            "decision": status,
            "reason": reason,
            "schedule_version": row.version,
            "contact_check": contact,
            "memory_manifest": memory,
            "task_id": str(occ.task_id) if occ.task_id else None,
            "covering_task_id": covering,
            "decision_mode": "deterministic approved-schedule policy",
        },
    )
    session.add(source)
    await session.flush()
    occ.decision_source_id = source.id
    event = await schedule_event(
        session,
        row,
        "supervisor.monthly_decision",
        {
            "occurrence_id": str(occ.id),
            "decision": status,
            "reason": reason,
            "source_id": str(source.id),
            "task_id": str(occ.task_id) if occ.task_id else None,
            "covering_task_id": covering,
        },
    )

    await wake_schedule(session, row, event)


async def cancel_occurrence_task(session, row, occ, reason):
    if not occ.task_id:
        return
    checkin = await session.scalar(
        select(ClientCheckIn).where(
            ClientCheckIn.tenant_id == row.tenant_id, ClientCheckIn.task_id == occ.task_id
        )
    )
    if checkin and checkin.sent_at is None:
        task = await session.get(Task, occ.task_id, with_for_update=True)
        if task.status not in TERMINAL:
            task.status = "CANCELLED"
            task.progress_json = {**task.progress_json, "last_reason": reason}
            event = await schedule_event(
                session,
                row,
                "monthly.suppressed",
                {"task_id": str(task.id), "month": occ.month_key, "reason": reason},
            )
            await wake_task(session, task, event)


async def delegate_due_monthly(database, tenant_id, case_id):
    """Supervisor-owned deterministic delegation of explicitly approved calendar occurrences."""
    from agents_platform.check_in_api import CreateCheckIn, create_check_in_in_transaction

    async with database.sessions.begin() as session:
        await policy_lock(session, tenant_id, publishing=True)
        case = await session.scalar(
            select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
        )
        if not case or case.status != "OPEN":
            return
        coord = await session.scalar(
            select(CaseCoordination)
            .where(CaseCoordination.tenant_id == tenant_id, CaseCoordination.case_id == case_id)
            .with_for_update()
        )
        if not coord:
            return
        pairs = (
            await session.execute(
                select(MonthlyCheckInOccurrence, MonthlyCheckInSchedule)
                .join(
                    MonthlyCheckInSchedule,
                    MonthlyCheckInSchedule.id == MonthlyCheckInOccurrence.schedule_id,
                )
                .where(
                    MonthlyCheckInOccurrence.tenant_id == tenant_id,
                    MonthlyCheckInOccurrence.case_id == case_id,
                    MonthlyCheckInOccurrence.status.in_(["DUE", "BLOCKED", "DELEGATED"]),
                )
                .order_by(MonthlyCheckInOccurrence.due_at)
                .limit(100)
            )
        ).all()
        for occ, row in pairs:
            if occ.task_id:
                checkin = await session.scalar(
                    select(ClientCheckIn).where(
                        ClientCheckIn.tenant_id == tenant_id, ClientCheckIn.task_id == occ.task_id
                    )
                )
                if checkin and checkin.sent_at:
                    continue
                task = await session.get(Task, occ.task_id)
                if task.status in TERMINAL:
                    continue
            if occ.expires_at <= now():
                await cancel_occurrence_task(
                    session, row, occ, "The monthly dispatch window ended."
                )
                await record_decision(
                    session,
                    row,
                    occ,
                    "EXPIRED",
                    "The monthly dispatch window ended; no catch-up message.",
                )
                continue
            if row.status != "ACTIVE":
                await record_decision(
                    session, row, occ, "BLOCKED", "Monthly schedule is paused or stopped."
                )
                continue
            if row.ends_at and row.ends_at <= now():
                await record_decision(session, row, occ, "EXPIRED", "Schedule end time reached.")
                continue
            if coord.status == "FAILED":
                await record_decision(
                    session, row, occ, "BLOCKED", "Case supervisor requires recovery."
                )
                continue
            assessment = await assess_contact(
                session, tenant_id, case_id, row.client_contact_id, current_task_id=occ.task_id
            )
            if assessment["code"] != "ELIGIBLE":
                status = (
                    "BLOCKED"
                    if assessment["code"] in ("OPEN_REVIEW", "CLIENT_SCOPE", "CASE_CLOSED")
                    else "SKIPPED"
                )
                if status == "SKIPPED":
                    await cancel_occurrence_task(session, row, occ, assessment["reason"])
                await record_decision(
                    session,
                    row,
                    occ,
                    status,
                    assessment["reason"],
                    contact=assessment,
                    covering=assessment.get("covering_task_id"),
                )
                continue
            group = await session.get(Task, row.group_task_id)
            memory = await memory_for_task(session, group)
            if stop_contact(memory):
                await record_decision(
                    session,
                    row,
                    occ,
                    "BLOCKED",
                    "An applicable case contact hold prevents this check-in.",
                    memory=memory["manifest"],
                )
                continue
            if occ.task_id:
                if occ.status == "BLOCKED":
                    event = await schedule_event(
                        session, row, "monthly.resumed_child", {"task_id": str(occ.task_id)}
                    )
                    await wake_task(session, task, event)
                await record_decision(
                    session,
                    row,
                    occ,
                    "DELEGATED",
                    "Supervisor reused the monthly task after rechecking eligibility.",
                    contact=assessment,
                    memory=memory["manifest"],
                )
                continue
            child = await create_check_in_in_transaction(
                session,
                tenant_id,
                CreateCheckIn(
                    case_id=case_id,
                    client_contact_id=row.client_contact_id,
                    parent_task_id=coord.group_task_id,
                    scheduled_at=occ.due_at,
                    objective=f"{row.objective} / {occ.month_key}",
                    message=row.message,
                ),
                f"monthly/{row.id}/{occ.month_key}",
                monthly_occurrence=occ,
            )
            occ.task_id = child.id
            await record_decision(
                session,
                row,
                occ,
                "DELEGATED",
                "Supervisor delegated this monthly occurrence to the client check-in specialist.",
                contact=assessment,
                memory=memory["manifest"],
            )


async def monthly_gate(session, checkin):
    if not checkin.monthly_occurrence_id or checkin.sent_at:
        return None
    occ = await session.get(MonthlyCheckInOccurrence, checkin.monthly_occurrence_id)
    row = await session.get(MonthlyCheckInSchedule, occ.schedule_id) if occ else None
    if (
        not occ
        or not row
        or occ.tenant_id != checkin.tenant_id
        or occ.case_id != checkin.case_id
        or occ.task_id != checkin.task_id
        or row.client_contact_id != checkin.client_contact_id
        or row.status != "ACTIVE"
    ):
        return "Monthly schedule is paused or stopped; do not send."
    if occ.expires_at <= now() or (row.ends_at and row.ends_at <= now()):
        return "Monthly dispatch window ended; do not send a catch-up message."
    return None


async def notify_monthly(session, task, event):
    checkin = await session.scalar(
        select(ClientCheckIn).where(
            ClientCheckIn.tenant_id == task.tenant_id, ClientCheckIn.task_id == task.id
        )
    )
    if not checkin or not checkin.monthly_occurrence_id:
        return
    occ = await session.get(MonthlyCheckInOccurrence, checkin.monthly_occurrence_id)
    row = await session.get(MonthlyCheckInSchedule, occ.schedule_id)
    if task.status == "COMPLETED":
        occ.status, occ.reason = "COMPLETED", "Check-in task completed with reply evidence."
    elif (
        task.status in ("FAILED", "CANCELLED")
        and row.status == "ACTIVE"
        and occ.status == "DELEGATED"
    ):
        row.status, row.version = "PAUSED", row.version + 1
        occ.status, occ.reason = (
            "BLOCKED",
            "Prior monthly task failed or was cancelled; inspect before resuming.",
        )
    await wake_schedule(session, row, event)


class MonthlyActivities:
    def __init__(self, database):
        self.database = database

    @activity.defn
    @observed_activity
    async def advance_monthly(self, activation):
        from agents_platform.coordination import enqueue_supervisor

        tenant, case_id, schedule_id = (
            UUID(activation[k]) for k in ("tenant_id", "case_id", "schedule_id")
        )
        async with self.database.sessions.begin() as session:
            await policy_lock(session, tenant, publishing=True)
            case = await session.scalar(
                select(Case).where(Case.tenant_id == tenant, Case.id == case_id).with_for_update()
            )
            row = await session.scalar(
                select(MonthlyCheckInSchedule)
                .where(
                    MonthlyCheckInSchedule.tenant_id == tenant,
                    MonthlyCheckInSchedule.case_id == case_id,
                    MonthlyCheckInSchedule.id == schedule_id,
                )
                .with_for_update()
            )
            if not row:
                return {"status": "CLOSED"}
            if row.status in ("STOPPED", "FINISHED"):
                return {"status": "CLOSED"}
            at = now()
            if not case or case.status != "OPEN" or (row.ends_at and row.ends_at <= at):
                row.status, row.next_due_at, row.version = "STOPPED", None, row.version + 1
                await cancel_unsent(session, row, "Case closed or schedule ended before send.")
                group = await session.get(Task, row.group_task_id)
                group.status, group.version = "CANCELLED", group.version + 1
                return {"status": "CLOSED"}
            if row.status == "PAUSED":
                return {
                    "status": "WAITING",
                    **({"wake_at": row.ends_at.isoformat()} if row.ends_at else {}),
                }
            due = []
            while (
                row.next_due_at
                and row.next_due_at <= at
                and row.occurrence_count < row.max_occurrences
            ):
                scheduled = row.next_due_at
                next_due = following_month(scheduled, row)
                deadline = min(scheduled + timedelta(hours=24), next_due, row.ends_at or next_due)
                missed = deadline <= at or scheduled < row.dispatch_after
                occ = MonthlyCheckInOccurrence(
                    id=uuid4(),
                    tenant_id=tenant,
                    case_id=case_id,
                    schedule_id=row.id,
                    month_key=scheduled.astimezone(ZoneInfo(row.timezone)).strftime("%Y-%m"),
                    due_at=scheduled,
                    expires_at=deadline,
                    status="MISSED" if missed else "DUE",
                    reason="Missed/paused occurrence; no catch-up message." if missed else None,
                    decision_revision=0,
                )
                session.add(occ)
                row.occurrence_count += 1
                row.next_due_at = next_due if row.occurrence_count < row.max_occurrences else None
                row.version += 1
                await session.flush()
                event = await schedule_event(
                    session,
                    row,
                    "monthly.missed" if missed else "monthly.due",
                    occurrence_view(occ),
                )
                if not missed:
                    await enqueue_supervisor(session, event)
                    due.append(occ)
            pending = (
                await session.scalars(
                    select(MonthlyCheckInOccurrence).where(
                        MonthlyCheckInOccurrence.tenant_id == tenant,
                        MonthlyCheckInOccurrence.schedule_id == row.id,
                        MonthlyCheckInOccurrence.status.in_(["DUE", "BLOCKED", "DELEGATED"]),
                    )
                )
            ).all()
            wake_dates = [d for d in (row.next_due_at, row.ends_at) if d and d > at]
            unfinished = False
            for occ in pending:
                checkin = await session.scalar(
                    select(ClientCheckIn).where(
                        ClientCheckIn.tenant_id == tenant,
                        ClientCheckIn.monthly_occurrence_id == occ.id,
                    )
                )
                if checkin and checkin.sent_at:
                    unfinished = (
                        True  # Its reply/review must finish before the final group completes.
                    )
                    continue
                if occ.expires_at <= at:
                    if occ.task_id:
                        task = await session.get(Task, occ.task_id, with_for_update=True)
                        if task.status not in TERMINAL:
                            task.status = "CANCELLED"
                            event = await schedule_event(
                                session,
                                row,
                                "monthly.expired",
                                {"task_id": str(task.id), "month": occ.month_key},
                            )
                            await wake_task(session, task, event)
                    occ.status, occ.reason = (
                        "EXPIRED",
                        "Dispatch window ended; no catch-up message.",
                    )
                else:
                    unfinished = True
                    wake_dates.append(occ.expires_at)
            if row.next_due_at is None and not unfinished:
                row.status, row.version = "FINISHED", row.version + 1
                group = await session.get(Task, row.group_task_id)
                group.status, group.version = "COMPLETED", group.version + 1
                await schedule_event(session, row, "monthly.finished", {})
                return {"status": "CLOSED"}
            return {
                "status": "WAITING",
                **({"wake_at": min(wake_dates).isoformat()} if wake_dates else {}),
            }

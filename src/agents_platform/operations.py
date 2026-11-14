"""Tenant-scoped operational signals, bounded drill-downs, and change-only log alerts."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, exists, func, or_, select

from agents_platform.models import (
    ActivityEvent,
    CaseCoordination,
    ClientCheckIn,
    Firm,
    HumanContinuation,
    HumanRequest,
    OutboxMessage,
    RecordsRequest,
    Task,
    TaskInbox,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.telemetry import emit

router = APIRouter(
    prefix="/v1/operator/firms/{tenant_id}",
    tags=["operations"],
    dependencies=[Depends(require_operator)],
)


def age(at, timestamp):
    return round(max(0, (at - timestamp).total_seconds()), 1) if timestamp else 0


async def snapshot(session, tenant_id, settings, at=None):
    at = at or datetime.now(timezone.utc)
    # A repeatable-read transaction at the caller keeps counts and samples consistent.
    pending = select(OutboxMessage).where(
        OutboxMessage.tenant_id == tenant_id, OutboxMessage.status == "PENDING"
    )
    pending_sq = pending.subquery()
    outbox_count, oldest = (
        await session.execute(
            select(func.count(), func.min(pending_sq.c.created_at)).select_from(pending_sq)
        )
    ).one()
    outbox_rows = (
        await session.execute(
            pending.join(
                ActivityEvent,
                (ActivityEvent.tenant_id == OutboxMessage.tenant_id)
                & (ActivityEvent.id == OutboxMessage.event_id),
            )
            .add_columns(ActivityEvent.case_id, ActivityEvent.task_id)
            .order_by(OutboxMessage.created_at, OutboxMessage.id)
            .limit(10)
        )
    ).all()

    # Queue delay is age of the oldest runnable, unread event, or initial pending task.
    # Exclude deliberate domain waits and human-blocked/terminal work.
    future_records = exists(
        select(RecordsRequest.id).where(
            RecordsRequest.task_id == Task.id,
            RecordsRequest.tenant_id == Task.tenant_id,
            or_(RecordsRequest.callback_at > at, RecordsRequest.next_reminder_at > at),
            RecordsRequest.status != "RECEIVED",
            Task.status != "RUNNING",
        )
    )
    future_checkin = exists(
        select(ClientCheckIn.id).where(
            ClientCheckIn.task_id == Task.id,
            ClientCheckIn.tenant_id == Task.tenant_id,
            ClientCheckIn.scheduled_at > at,
        )
    )
    future_continuation = exists(
        select(HumanContinuation.id).where(
            HumanContinuation.task_id == Task.id,
            HumanContinuation.tenant_id == Task.tenant_id,
            Task.status != "RUNNING",
            or_(
                and_(HumanContinuation.status == "PENDING", HumanContinuation.ready_at > at),
                and_(HumanContinuation.status == "APPLIED", HumanContinuation.response_due_at > at),
            ),
            ~exists(
                select(RecordsRequest.id).where(
                    RecordsRequest.tenant_id == Task.tenant_id,
                    RecordsRequest.task_id == Task.id,
                    RecordsRequest.status == "RECEIVED",
                )
            ),
        )
    )
    inbox = (
        select(
            TaskInbox.task_id.label("task_id"), func.min(TaskInbox.created_at).label("queued_at")
        )
        .where(TaskInbox.tenant_id == tenant_id, TaskInbox.consumed_by_execution_id.is_(None))
        .group_by(TaskInbox.task_id)
        .subquery()
    )
    queued_at = func.coalesce(inbox.c.queued_at, Task.created_at)
    work = (
        select(Task, queued_at.label("queued_at"))
        .outerjoin(inbox, inbox.c.task_id == Task.id)
        .where(
            Task.tenant_id == tenant_id,
            Task.kind == "AGENT",
            Task.status.in_(["PENDING", "RUNNING", "WAITING"]),
            or_(inbox.c.task_id.is_not(None), Task.status == "PENDING"),
            ~future_records,
            ~future_checkin,
            ~future_continuation,
        )
    )
    work_sq = work.subquery()
    worker_count, worker_oldest = (
        await session.execute(
            select(func.count(), func.min(work_sq.c.queued_at)).select_from(work_sq)
        )
    ).one()
    work_rows = (await session.execute(work.order_by(queued_at, Task.id).limit(10))).all()

    failed_supervisor = exists(
        select(CaseCoordination.id).where(
            CaseCoordination.tenant_id == Task.tenant_id,
            CaseCoordination.group_task_id == Task.id,
            CaseCoordination.status == "FAILED",
        )
    )
    failed = select(Task).where(
        Task.tenant_id == tenant_id, or_(Task.status == "FAILED", failed_supervisor)
    )
    callback = (
        select(Task, RecordsRequest.callback_at)
        .join(
            RecordsRequest,
            (RecordsRequest.task_id == Task.id) & (RecordsRequest.tenant_id == Task.tenant_id),
        )
        .where(
            Task.tenant_id == tenant_id,
            Task.status.not_in(["COMPLETED", "FAILED", "CANCELLED"]),
            RecordsRequest.status != "RECEIVED",
            RecordsRequest.callback_at
            <= at - timedelta(seconds=settings.ops_callback_grace_seconds),
        )
    )
    human = select(HumanRequest).where(
        HumanRequest.tenant_id == tenant_id, HumanRequest.status == "OPEN"
    )
    failed_count = await session.scalar(select(func.count()).select_from(failed.subquery()))
    callback_count = await session.scalar(select(func.count()).select_from(callback.subquery()))
    human_sq = human.subquery()
    human_count, human_oldest = (
        await session.execute(
            select(func.count(), func.min(human_sq.c.created_at)).select_from(human_sq)
        )
    ).one()
    human_overdue = await session.scalar(
        select(func.count()).select_from(human.where(HumanRequest.due_at < at).subquery())
    )
    failed_rows = (await session.scalars(failed.order_by(Task.created_at, Task.id).limit(10))).all()
    callback_rows = (
        await session.execute(callback.order_by(RecordsRequest.callback_at, Task.id).limit(10))
    ).all()
    human_rows = (
        await session.scalars(human.order_by(HumanRequest.created_at, HumanRequest.id).limit(10))
    ).all()

    def task_item(task, **extra):
        return {
            "task_id": str(task.id),
            "case_id": str(task.case_id),
            "workflow_id": task.workflow_id,
            "status": task.status,
            **extra,
        }

    signals = [
        {
            "key": "outbox",
            "label": "Pending outbox",
            "count": outbox_count,
            "age_seconds": age(at, oldest),
            "threshold_seconds": settings.ops_outbox_age_seconds,
            "alert": bool(outbox_count and age(at, oldest) >= settings.ops_outbox_age_seconds),
            "items": [
                {
                    "outbox_id": str(r.id),
                    "event_id": str(r.event_id),
                    "task_id": str(task),
                    "case_id": str(case),
                    "workflow_id": r.destination_key,
                    "attempts": r.attempts,
                    "age_seconds": age(at, r.created_at),
                    "next_attempt_at": r.available_at.isoformat(),
                }
                for r, case, task in outbox_rows
            ],
        },
        {
            "key": "worker",
            "label": "Runnable work delay",
            "count": worker_count,
            "age_seconds": age(at, worker_oldest),
            "threshold_seconds": settings.ops_worker_delay_seconds,
            "alert": bool(
                worker_count and age(at, worker_oldest) >= settings.ops_worker_delay_seconds
            ),
            "items": [task_item(t, age_seconds=age(at, queued)) for t, queued in work_rows],
        },
        {
            "key": "failed",
            "label": "Failed tasks",
            "count": failed_count,
            "alert": bool(failed_count),
            "items": [task_item(t, status="FAILED") for t in failed_rows],
        },
        {
            "key": "callbacks",
            "label": "Overdue callbacks",
            "count": callback_count,
            "threshold_seconds": settings.ops_callback_grace_seconds,
            "alert": bool(callback_count),
            "items": [
                task_item(t, callback_at=due.isoformat(), age_seconds=age(at, due))
                for t, due in callback_rows
            ],
        },
        {
            "key": "human",
            "label": "Unresolved human requests",
            "count": human_count,
            "overdue_count": human_overdue,
            "age_seconds": age(at, human_oldest),
            "threshold_seconds": settings.ops_review_age_seconds,
            "alert": bool(
                human_overdue
                or (human_count and age(at, human_oldest) >= settings.ops_review_age_seconds)
            ),
            "items": [
                {
                    "task_id": str(r.task_id),
                    "case_id": str(r.case_id),
                    "request_id": str(r.id),
                    "age_seconds": age(at, r.created_at),
                    "due_at": r.due_at.isoformat() if r.due_at else None,
                }
                for r in human_rows
            ],
        },
    ]
    return {
        "firm_id": str(tenant_id),
        "as_of": at.isoformat(),
        "alert_count": sum(s["alert"] for s in signals),
        "signals": signals,
    }


async def consistent_snapshot(database, tenant_id, settings):
    async with database.sessions() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return await snapshot(session, tenant_id, settings)


@router.get("/operations")
async def operations(tenant_id: UUID, request: Request, session: Session):
    if await session.get(Firm, tenant_id) is None:
        raise HTTPException(404, "Firm not found")
    return await consistent_snapshot(
        request.app.state.database, tenant_id, request.app.state.settings
    )


class AlertTransitions:
    def __init__(self):
        self.active = set()

    def update(self, result):
        firm = result["firm_id"]
        current = {(firm, s["key"]) for s in result["signals"] if s["alert"]}
        previous = {key for key in self.active if key[0] == firm}
        for key in sorted(current - previous):
            signal = next(s for s in result["signals"] if s["key"] == key[1])
            sample = signal["items"][0] if signal["items"] else {}
            emit(
                "operations.alert_opened",
                logging.WARNING,
                firm_id=firm,
                alert_key=key[1],
                count=signal["count"],
                age_seconds=signal.get("age_seconds"),
                **{
                    k: sample[k]
                    for k in ("case_id", "task_id", "event_id", "workflow_id")
                    if k in sample
                },
            )
        for key in sorted(previous - current):
            emit("operations.alert_cleared", firm_id=firm, alert_key=key[1])
        self.active = (self.active - previous) | current


async def monitor_loop(database, settings):
    transitions = AlertTransitions()
    unhealthy = False
    while True:
        try:
            async with database.sessions() as session:
                firms = (await session.scalars(select(Firm.id))).all()
            for firm in firms:
                transitions.update(await consistent_snapshot(database, firm, settings))
            if unhealthy:
                emit("operations.monitor_recovered")
            unhealthy = False
        except Exception as exc:
            if not unhealthy:
                emit("operations.monitor_failed", logging.ERROR, error_type=type(exc).__name__)
            unhealthy = True
        await asyncio.sleep(settings.ops_poll_seconds)

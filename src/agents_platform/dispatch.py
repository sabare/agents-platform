"""Deliver saved outbox messages. Task scheduling itself belongs to Temporal."""

import asyncio
import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from agents_platform.execution import now
from agents_platform.models import (
    ActivityEvent,
    CaseCoordination,
    CheckInSeries,
    MonthlyCheckInSchedule,
    OutboxMessage,
    Task,
)
from agents_platform.orchestration.workflows import (
    CaseWorkflow,
    CheckInSeriesWorkflow,
    MonthlyCheckInWorkflow,
    TaskWorkflow,
)
from agents_platform.telemetry import emit


async def dispatch_once(database, client, task_queue: str, tenant_id=None) -> bool:
    # One short transaction locks one delivery row, not the business task/model run.
    async with database.sessions.begin() as session:
        query = select(OutboxMessage)
        if tenant_id is not None:
            query = query.where(OutboxMessage.tenant_id == tenant_id)
        message = await session.scalar(
            query.where(OutboxMessage.status == "PENDING", OutboxMessage.available_at <= now())
            .order_by(OutboxMessage.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if message is None:
            return False
        source_event = await session.get(ActivityEvent, message.event_id)
        try:
            if message.destination == "temporal.wake_monthly":
                row = await session.scalar(
                    select(MonthlyCheckInSchedule).where(
                        MonthlyCheckInSchedule.tenant_id == UUID(message.payload["tenant_id"]),
                        MonthlyCheckInSchedule.case_id == UUID(message.payload["case_id"]),
                        MonthlyCheckInSchedule.id == UUID(message.payload["schedule_id"]),
                    )
                )
                if row is None or row.workflow_id != message.destination_key:
                    raise ValueError("Outbox monthly schedule scope mismatch")
                target, active = MonthlyCheckInWorkflow.run, True
            elif message.destination == "temporal.wake_series":
                series = await session.scalar(
                    select(CheckInSeries).where(
                        CheckInSeries.tenant_id == UUID(message.payload["tenant_id"]),
                        CheckInSeries.case_id == UUID(message.payload["case_id"]),
                        CheckInSeries.id == UUID(message.payload["series_id"]),
                    )
                )
                if series is None or series.workflow_id != message.destination_key:
                    raise ValueError("Outbox series scope mismatch")
                target, active = CheckInSeriesWorkflow.run, True
            elif message.destination == "temporal.signal_task":
                task = await session.get(Task, UUID(message.payload["task_id"]))
                if (
                    task is None
                    or str(task.tenant_id) != message.payload["tenant_id"]
                    or task.workflow_id != message.destination_key
                ):
                    raise ValueError("Outbox task scope mismatch")
                try:
                    await client.get_workflow_handle(message.destination_key).signal(
                        "wake", rpc_timeout=timedelta(seconds=3)
                    )
                except RPCError as exc:
                    if exc.status != RPCStatusCode.NOT_FOUND:
                        raise
                message.status = "DELIVERED"
                return True
            elif message.destination == "temporal.wake_case":
                coord = await session.scalar(
                    select(CaseCoordination).where(
                        CaseCoordination.tenant_id == UUID(message.payload["tenant_id"]),
                        CaseCoordination.case_id == UUID(message.payload["case_id"]),
                    )
                )
                if coord is None or coord.workflow_id != message.destination_key:
                    raise ValueError("Outbox case scope mismatch")
                target, active = CaseWorkflow.run, True
            elif message.destination in ("temporal.start_task", "temporal.wake_task"):
                task = await session.get(Task, UUID(message.payload["task_id"]))
                if task is None or str(task.tenant_id) != message.payload["tenant_id"]:
                    raise ValueError("Outbox task scope mismatch")
                if task.workflow_id != message.destination_key:
                    message.status = "DELIVERED"  # Saved wake belongs to a superseded generation.
                    return True
                generation = task.progress_json.get("recovery_generation", 0)
                if generation:
                    message.payload = {
                        **message.payload,
                        "generation": generation,
                        "cycle": task.progress_json["recovery_cycle"],
                        "trigger": "recovery",
                    }
                target = TaskWorkflow.run
                active = task.status not in ("COMPLETED", "FAILED", "CANCELLED")
            else:
                raise ValueError("Unknown outbox destination")
            if active:
                try:
                    await client.start_workflow(
                        target,
                        message.payload,
                        id=message.destination_key,
                        task_queue=task_queue,
                        start_signal="wake",
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        rpc_timeout=timedelta(seconds=3),
                    )
                except WorkflowAlreadyStartedError:
                    pass
            message.status = "DELIVERED"
        except Exception as exc:
            message.attempts += 1
            message.available_at = now() + timedelta(seconds=min(30, 2 ** min(message.attempts, 5)))
            emit(
                "outbox.delivery_failed",
                logging.WARNING,
                firm_id=message.tenant_id,
                task_id=source_event.task_id if source_event else message.payload.get("task_id"),
                case_id=source_event.case_id if source_event else message.payload.get("case_id"),
                event_id=message.event_id,
                workflow_id=message.destination_key,
                outbox_id=message.id,
                attempts=message.attempts,
                error_type=type(exc).__name__,
            )
        return True


async def dispatch_loop(database, client, task_queue: str):
    while True:
        try:
            delivered = await dispatch_once(database, client, task_queue)
        except Exception as exc:
            emit("outbox.unavailable", logging.ERROR, error_type=type(exc).__name__)
            delivered = False
        await asyncio.sleep(0.05 if delivered else 0.5)

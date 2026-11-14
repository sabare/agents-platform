"""JSON lifecycle logs with task correlation; never serialize model or provider content."""

import functools
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.orm import Session

_CONTEXT = ContextVar("log_context", default={})
IDS = ("request_id", "firm_id", "case_id", "task_id", "execution_id", "event_id", "workflow_id")
LOG = logging.getLogger("agents_platform.telemetry")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        fields = {**dict.fromkeys(IDS), **_CONTEXT.get(), **getattr(record, "fields", {})}
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **fields,
            },
            default=str,
        )


def configure_logging(level="INFO"):
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(level)
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
    # Uvicorn's access logger includes the raw query string; middleware logs the route instead.
    logging.getLogger("uvicorn.access").disabled = True


@contextmanager
def log_context(**fields):
    token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def bind_context(**fields):
    _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})


def emit(name, level=logging.INFO, **fields):
    LOG.log(
        level,
        name,
        extra={"fields": {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}},
    )


def observed_activity(function):
    @functools.wraps(function)
    async def wrapped(self, activation):
        from temporalio import activity

        fields = {
            "firm_id": activation.get("tenant_id"),
            "case_id": activation.get("case_id"),
            "task_id": activation.get("task_id"),
            "activity": function.__name__,
            "cycle": activation.get("cycle"),
            "trigger": activation.get("trigger"),
        }
        if activity.in_activity():
            info = activity.info()
            fields.update(
                workflow_id=info.workflow_id,
                attempt=info.attempt,
                schedule_to_start_ms=max(
                    0,
                    (info.started_time - info.current_attempt_scheduled_time).total_seconds()
                    * 1000,
                ),
            )
        with log_context(**fields):
            started = time.monotonic()
            emit("activity.started")
            try:
                result = await function(self, activation)
                emit(
                    "activity.finished",
                    status=result.get("status") if isinstance(result, dict) else None,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                return result
            except Exception as exc:
                emit(
                    "activity.failed",
                    logging.ERROR,
                    error_type=type(exc).__name__,
                    duration_ms=round((time.monotonic() - started) * 1000, 2),
                )
                raise

    return wrapped


@event.listens_for(Session, "after_flush")
def collect_committed_changes(session, flush_context):
    from agents_platform.models import (
        ActivityEvent,
        CaseCoordination,
        Execution,
        OutboxMessage,
        Task,
        ToolInvocation,
    )

    pending = session.info.setdefault("telemetry", {})
    for row in list(session.new) + list(session.dirty):
        if not isinstance(row, (ActivityEvent, Execution, OutboxMessage, ToolInvocation)):
            continue
        fields = {
            "firm_id": row.tenant_id,
            "case_id": getattr(row, "case_id", None),
            "task_id": getattr(row, "task_id", None),
        }
        if isinstance(row, ActivityEvent):
            fields.update(
                event_id=row.id,
                event_type=row.event_type,
                execution_id=row.payload.get("execution_id"),
            )
            name = "event.committed"
        elif isinstance(row, Execution):
            fields.update(
                execution_id=row.id,
                status=row.status,
                attempt=row.attempt,
                error_type=row.error_type,
            )
            name = "execution.committed"
        elif isinstance(row, ToolInvocation):
            fields.update(
                execution_id=row.execution_id, tool=row.tool_name, action_key=row.logical_key
            )
            name = "tool.committed"
        else:
            fields.update(
                event_id=row.event_id,
                workflow_id=row.destination_key,
                outbox_id=row.id,
                destination=row.destination,
                status=row.status,
                attempts=row.attempts,
            )
            name = "outbox.committed"
        connection = session.connection()
        if isinstance(row, OutboxMessage):
            linked = connection.execute(
                select(ActivityEvent.case_id, ActivityEvent.task_id).where(
                    ActivityEvent.tenant_id == row.tenant_id, ActivityEvent.id == row.event_id
                )
            ).first()
            if linked:
                fields.update(case_id=linked.case_id, task_id=linked.task_id)
        if fields.get("task_id"):
            task = connection.execute(
                select(Task.case_id, Task.workflow_id).where(
                    Task.tenant_id == row.tenant_id, Task.id == fields["task_id"]
                )
            ).first()
            if task:
                fields["case_id"] = task.case_id
                workflow_id = task.workflow_id or connection.scalar(
                    select(CaseCoordination.workflow_id).where(
                        CaseCoordination.tenant_id == row.tenant_id,
                        CaseCoordination.group_task_id == fields["task_id"],
                    )
                )
                if workflow_id:
                    fields.setdefault("workflow_id", workflow_id)
        pending[(name, str(row.id))] = (name, fields)


@event.listens_for(Session, "after_commit")
def log_committed_changes(session):
    for name, fields in session.info.pop("telemetry", {}).values():
        emit(name, **fields)


@event.listens_for(Session, "after_rollback")
def discard_changes(session):
    session.info.pop("telemetry", None)

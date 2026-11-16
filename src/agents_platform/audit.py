"""A small common journal. Business evidence stays in its existing tables."""

import functools
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import event, text

_CONTEXT = ContextVar("audit_context", default={})
_TOOL = ContextVar("audit_tool", default=None)


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str = Field(min_length=1, max_length=100)
    tenant_id: UUID | None = None
    case_id: UUID | None = None
    task_id: UUID | None = None
    execution_id: UUID | None = None
    actor_ref: str = Field(default="platform", min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=200)
    resource_type: str | None = None
    resource_id: str | None = None
    outcome: Literal[
        "ATTEMPTED", "ALLOWED", "DENIED", "SUCCEEDED", "FAILED", "REJECTED", "CACHED", "PROPOSED"
    ]
    reason_code: str = Field(min_length=1, max_length=100)
    policy_version: str | None = None
    request_id: UUID | None = None
    trace_id: str | None = None
    span_id: str | None = None
    attempt_id: UUID | None = None
    details: dict = Field(default_factory=dict)


@contextmanager
def audit_scope(database, **identity):
    token = _CONTEXT.set({"database": database, **identity})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def bind_audit(**identity):
    _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in identity.items() if v is not None}})


def correlation():
    from agents_platform.observability import trace_reference

    ctx = {k: v for k, v in _CONTEXT.get().items() if k != "database"}
    ref = trace_reference()
    if ref:
        ctx.update(trace_id=ref["trace_id"], span_id=ref["span_id"])
    return ctx


def make_event(**fields):
    context = correlation()
    return AuditEvent(**{**context, **fields})


async def append_event(session, entry):
    """Call in the business transaction when evidence must commit with the effect."""
    return await session.scalar(
        text("SELECT audit.append_event(CAST(:event AS jsonb))"),
        {"event": entry.model_dump_json()},
    )


async def persist(database, *entries):
    # Separate short transaction: rejected/failed attempts survive business rollback.
    # Failure propagates. Audit is durable evidence, unlike best-effort tracing.
    async with database.sessions.begin() as session:
        for entry in entries:
            await append_event(session, entry)


def instrument_database(engine):
    @event.listens_for(engine.sync_engine, "begin")
    def set_identity(connection):
        connection.execute(
            text("SELECT set_config('app.audit_context', :context, true)"),
            {"context": json.dumps(correlation(), default=str)},
        )


def cached_result():
    state = _TOOL.get()
    if state is not None:
        state["cached"] = True


def reject(reason_code):
    state = _TOOL.get()
    if state is not None:
        state.update(allowed=False, reason_code=reason_code)


async def authorize_tool(session, *, policy_version, transactional=True, **details):
    """Record the actual backend gate before an effect, in the same transaction."""
    state = _TOOL.get()
    if state is not None:
        state.update(allowed=True, reason_code="POLICY_PASSED", policy_version=policy_version)
        if not transactional:
            return
        await append_event(
            session,
            make_event(
                event_type="authorization.decision",
                action=state["name"],
                outcome="ALLOWED",
                reason_code="POLICY_PASSED",
                policy_version=policy_version,
                details=details,
            ),
        )
        state["decision_committed_with_effect"] = True


async def invoke_tool(name, callback, context, raw_arguments):
    database = _CONTEXT.get().get("database")
    if database is None:
        return await callback(context, raw_arguments)
    state = {"name": name, "allowed": None, "reason_code": "TOOL_ERROR"}
    token = _TOOL.set(state)
    identity_token = _CONTEXT.set({**_CONTEXT.get(), "attempt_id": uuid4()})
    try:
        await persist(
            database,
            make_event(
                event_type="tool.attempt",
                action=name,
                outcome="ATTEMPTED",
                reason_code="INVOKED",
            ),
        )
        try:
            result = await callback(context, raw_arguments)
        except BaseException as exc:
            from pydantic import ValidationError

            from agents_platform.domains import ActionNotDue, ActionStateChanged

            if isinstance(exc, ValidationError):
                reject("INVALID_ARGUMENTS")
            elif isinstance(exc, ActionNotDue):
                reject("ACTION_NOT_DUE")
            elif isinstance(exc, ActionStateChanged):
                reject("ACTION_STATE_CHANGED")
            entries = []
            # A rolled-back ALLOWED decision is also re-recorded. Never imply that
            # a later adapter/provider error was an authorization denial.
            if state["allowed"] is not None:
                entries.append(
                    make_event(
                        event_type="authorization.decision",
                        action=name,
                        outcome="ALLOWED" if state["allowed"] else "DENIED",
                        reason_code=state["reason_code"],
                        policy_version=state.get("policy_version"),
                    )
                )
            entries.append(
                make_event(
                    event_type="tool.result",
                    action=name,
                    outcome="REJECTED" if state["allowed"] is False else "FAILED",
                    reason_code=state["reason_code"],
                    details={"error_type": type(exc).__name__},
                )
            )
            await persist(database, *entries)
            raise
        else:
            try:
                payload = json.loads(result) if isinstance(result, str) else result
            except (ValueError, TypeError):
                payload = None
            rejected = isinstance(payload, dict) and (
                "error" in payload or payload.get("action_taken") is False
            )
            outcome = "REJECTED" if rejected else "CACHED" if state.get("cached") else "SUCCEEDED"
            if isinstance(payload, dict) and payload.get("status") == "PROPOSED":
                outcome = "PROPOSED"
            entries = []
            if state["allowed"] is True and not state.get("decision_committed_with_effect"):
                entries.append(
                    make_event(
                        event_type="authorization.decision",
                        action=name,
                        outcome="ALLOWED",
                        reason_code=state["reason_code"],
                        policy_version=state.get("policy_version"),
                    )
                )
            if state["allowed"] is False or (rejected and state["allowed"] is not True):
                entries.append(
                    make_event(
                        event_type="authorization.decision",
                        action=name,
                        outcome="DENIED",
                        reason_code=state["reason_code"]
                        if state["allowed"] is False
                        else "TOOL_REJECTED",
                        policy_version=state.get("policy_version"),
                    )
                )
            await persist(
                database,
                *entries,
                make_event(
                    event_type="tool.result",
                    action=name,
                    outcome=outcome,
                    reason_code="TOOL_REJECTED" if rejected else "RETURNED",
                ),
            )
            return result
    finally:
        _CONTEXT.reset(identity_token)
        _TOOL.reset(token)


def audited_activity(function):
    @functools.wraps(function)
    async def wrapped(self, activation):
        # The shared lifecycle logger also wraps infrastructure/test activities that
        # have no durable business database. Keep those usable independently.
        database = getattr(self, "database", None)
        if database is None:
            return await function(self, activation)
        identity = {k: activation.get(k) for k in ("tenant_id", "case_id", "task_id")}
        with audit_scope(self.database, actor_ref="platform/worker", **identity):
            try:
                return await function(self, activation)
            except BaseException as exc:
                await persist(
                    self.database,
                    make_event(
                        event_type="action.result",
                        action=function.__name__,
                        outcome="FAILED",
                        reason_code="ACTIVITY_ERROR",
                        details={"error_type": type(exc).__name__},
                    ),
                )
                raise

    return wrapped

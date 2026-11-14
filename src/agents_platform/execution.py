"""One bounded activation: saved context → SDK/tools → validated, saved outcome.

Provider tools are database-backed simulations. ScriptedModel replaces only model
responses; both modes use the same SDK runner, tools, evidence, and commit path.
"""

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from agents import Agent, FunctionTool, ModelSettings, RunConfig, Runner
from sqlalchemy import select
from temporalio import activity

from agents_platform.config import Settings
from agents_platform.context import (
    PLATFORM_INSTRUCTIONS,
    ContextBudgetExceeded,
    ExecutionRetrieval,
    ModelBudget,
)
from agents_platform.db import Database
from agents_platform.domains import DomainAdapter, adapter_ref, default_registry
from agents_platform.memory import memory_for_task, policy_lock, stop_contact
from agents_platform.memory_history import historical_context
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Execution,
    Source,
    Task,
    TaskInbox,
    ToolInvocation,
)
from agents_platform.task_contracts import NextAction, TaskSnapshot

TERMINAL = {"COMPLETED", "CANCELLED", "FAILED"}


def now():
    return datetime.now(timezone.utc)


class StaleExecution(Exception):
    """A newer task update or retry owns this work now."""


@dataclass
class ActivationContext:
    tenant_id: UUID
    task_id: UUID
    execution_id: UUID
    attempt: int
    context: dict
    definition: dict
    mode: str
    adapter: DomainAdapter
    tools_used: list = field(default_factory=list)
    evidence_source_ids: set[str] = field(default_factory=set)
    retrieved_source_ids: set[str] = field(default_factory=set)


class TaskActivities:
    def __init__(self, database: Database, settings: Settings, adapters=None):
        self.database, self.settings = database, settings
        self.adapters = adapters or default_registry()

    @activity.defn
    async def execute_task(self, activation: dict) -> dict:
        """The complete activation in three steps, with no database lock during model calls."""
        run = await self._prepare(activation)
        if isinstance(run, dict):
            return run  # Previously committed result, closed task, or duplicate signal.
        decision, usage = await self._decide(run, self._tools(run))
        return await self._commit(run, activation, decision, usage)

    async def _prepare(self, activation: dict):
        tenant_id, task_id = UUID(activation["tenant_id"]), UUID(activation["task_id"])
        execution_id = uuid5(task_id, f"execution/{activation['cycle']}")
        async with self.database.sessions.begin() as session:
            await policy_lock(session, tenant_id)
            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == tenant_id, Task.id == task_id)
                .with_for_update()
            )
            if task is None:
                raise ValueError("Task not found")
            if task.status in ("CANCELLED", "FAILED"):
                return {"status": task.status}
            execution = await session.get(Execution, execution_id)
            if execution is not None and execution.outcome_json is not None:
                return execution.outcome_json
            if task.status == "COMPLETED":
                return {"status": task.status}
            inbox = (
                await session.scalars(
                    select(TaskInbox).where(
                        TaskInbox.tenant_id == tenant_id,
                        TaskInbox.task_id == task_id,
                        TaskInbox.consumed_by_execution_id.is_(None),
                    )
                )
            ).all()
            # Duplicate wake-ups do not create new model runs or extend the timer.
            if activation["trigger"] == "event" and not inbox and execution is None:
                previous = await session.scalar(
                    select(Execution)
                    .where(
                        Execution.tenant_id == tenant_id,
                        Execution.task_id == task_id,
                        Execution.status == "SUCCEEDED",
                    )
                    .order_by(Execution.cycle.desc())
                    .limit(1)
                )
                if previous:
                    return previous.outcome_json
            agent = await session.get(AgentVersion, (task.agent_key, task.agent_version))
            if agent is None:
                raise ValueError("Published agent definition missing")
            definition = agent.definition_snapshot
            adapter = self.adapters.for_execution(task, definition)
            if execution is None:
                domain = await adapter.load(session, task)
                if domain.ready_at and domain.ready_at > now():
                    if task.status != "WAITING":
                        task.status, task.version = "WAITING", task.version + 1
                    return {
                        "status": "WAITING",
                        "wake_at": domain.ready_at.isoformat(),
                        "reason": "Scheduled task is not due yet.",
                    }
                task.status = "RUNNING"
                task.version += 1
                view = TaskSnapshot(
                    **{name: getattr(task, name) for name in TaskSnapshot.model_fields}
                ).model_dump(mode="json")
                if set(domain.task_fields) & set(TaskSnapshot.model_fields):
                    raise ValueError("Domain projection cannot overwrite platform task fields")
                view.update(domain.task_fields)
                memory = await memory_for_task(session, task, adapter=adapter, snapshot=domain)
                history = await historical_context(session, tenant_id, task.case_id, task.objective)
                recent = (
                    await session.scalars(
                        select(ActivityEvent)
                        .where(
                            ActivityEvent.tenant_id == tenant_id, ActivityEvent.task_id == task_id
                        )
                        .order_by(ActivityEvent.created_at.desc())
                        .limit(10)
                    )
                ).all()
                unread_events = (
                    await session.scalars(
                        select(ActivityEvent).where(
                            ActivityEvent.tenant_id == tenant_id,
                            ActivityEvent.id.in_([entry.event_id for entry in inbox]),
                        )
                    )
                ).all()
                source_ids = {
                    e.payload["source_id"]
                    for e in [*recent, *unread_events]
                    if "source_id" in e.payload
                }
                sources = (
                    await session.scalars(
                        select(Source).where(
                            Source.tenant_id == tenant_id,
                            Source.case_id == task.case_id,
                            Source.id.in_([UUID(x) for x in source_ids]),
                        )
                    )
                ).all()
                context = {
                    "task": view,
                    "instructions": memory["instructions"],
                    "memory": memory,
                    "history": history,
                    "domain": {"adapter": adapter_ref(adapter), "state": domain.state},
                    "action_requirements": {
                        **domain.action_requirements,
                        "current_memory_is_authoritative": True,
                        "resolved_items_are_history": True,
                    },
                    "recent_events": [
                        {"type": e.event_type, "payload": e.payload} for e in reversed(recent)
                    ],
                    "new_events": [
                        {"type": e.event_type, "payload": e.payload} for e in unread_events
                    ],
                    "sources": [{"id": str(s.id), "content": s.content} for s in sources],
                    "inbox_ids": [str(i.id) for i in inbox],
                    "expected_version": task.version,
                    "agent_definition_hash": agent.definition_hash,
                    "deadline": (now() + timedelta(minutes=5)).isoformat(),
                }
                execution = Execution(
                    id=execution_id,
                    tenant_id=tenant_id,
                    case_id=task.case_id,
                    task_id=task_id,
                    cycle=activation["cycle"],
                    mode=self.settings.agent_mode,
                    status="RUNNING",
                    attempt=1,
                    context_json=context,
                )
                session.add(execution)
            else:
                execution.attempt += 1
                execution.status = "RUNNING"
                # Retry the saved context, not an unrecorded replacement snapshot.
            attempt = execution.attempt
            context = execution.context_json
            if "domain" in context:
                self.adapters.for_execution(task, definition, context["domain"]["adapter"])
            mode = execution.mode
            if (
                "memory" not in context
                or "domain" not in context
                or context["expected_version"] != task.version
            ):
                execution.status = "STALE"
                execution.finished_at = now()
                execution.outcome_json = {"status": "CONTINUE"}
                return execution.outcome_json
            await session.flush()
        return ActivationContext(
            tenant_id, task_id, execution_id, attempt, context, definition, mode, adapter
        )

    def _tools(self, run):
        tenant_id, task_id, execution_id = run.tenant_id, run.task_id, run.execution_id
        context, definition, attempt = run.context, run.definition, run.attempt
        run_context = run
        tools_used = run.tools_used
        run_adapter = run.adapter

        async def tool(spec, raw_arguments):
            name = spec.name
            arguments = spec.arguments.model_validate_json(raw_arguments)
            arguments_json = arguments.model_dump(mode="json")
            if name not in definition["tools"]:
                raise ValueError("Tool is not permitted by this agent version")
            if len(tools_used) >= definition["limits"]["max_tool_calls"]:
                raise ValueError("Tool budget exceeded")
            async with self.database.sessions.begin() as session:
                await policy_lock(session, tenant_id)
                current = await session.scalar(
                    select(Task)
                    .where(Task.id == task_id, Task.tenant_id == tenant_id)
                    .with_for_update()
                )
                run = await session.get(Execution, execution_id)
                if run.attempt != attempt or current.version != context["expected_version"]:
                    raise StaleExecution()
                self.adapters.for_execution(current, definition, context["domain"]["adapter"])
                memory = await memory_for_task(session, current, adapter=run_adapter)
                if (
                    memory["manifest"]["fingerprint"]
                    != context["memory"]["manifest"]["fingerprint"]
                ):
                    raise StaleExecution()
                if spec.effect == "contact" and stop_contact(memory):
                    raise StaleExecution()
                if current.status != "RUNNING":
                    raise StaleExecution()
                argument_hash = hashlib.sha256(
                    json.dumps(arguments_json, sort_keys=True).encode()
                ).hexdigest()
                action_key = spec.task_key(arguments) if callable(spec.task_key) else spec.task_key
                key = action_key or (
                    f"{execution_id}/{name}"
                    if not arguments_json
                    else f"{execution_id}/{name}/{argument_hash}"
                )
                cached = await session.scalar(
                    select(ToolInvocation).where(
                        ToolInvocation.tenant_id == tenant_id,
                        ToolInvocation.task_id == task_id,
                        ToolInvocation.logical_key == key,
                    )
                )
                if cached:
                    if cached.tool_name != name or cached.arguments_json != arguments_json:
                        raise ValueError(
                            "Logical tool action was already used with different arguments"
                        )
                    result = cached.result_json
                    evidence_ids = result.get("_evidence_source_ids", [])
                else:
                    domain_result = await run_adapter.execute_tool(
                        session, current, name, arguments
                    )
                    result = domain_result.data
                    evidence_ids = [str(i) for i in domain_result.evidence_source_ids]
                    source = Source(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        case_id=current.case_id,
                        source_key=f"tool/{task_id}/{key}",
                        kind=domain_result.source_kind,
                        content=result,
                    )
                    session.add(source)
                    await session.flush()
                    if domain_result.event_type:
                        session.add(
                            ActivityEvent(
                                tenant_id=tenant_id,
                                case_id=current.case_id,
                                task_id=task_id,
                                event_type=domain_result.event_type,
                                producer="domain-tool",
                                dedupe_key=f"{task_id}/{key}",
                                actor_ref=f"agent/{current.agent_key}/{current.agent_version}",
                                payload={"source_id": str(source.id), "tool": name, **result},
                            )
                        )
                    result = {
                        **result,
                        "source_id": str(source.id),
                        "_evidence_source_ids": evidence_ids,
                    }
                    session.add(
                        ToolInvocation(
                            tenant_id=tenant_id,
                            case_id=current.case_id,
                            task_id=task_id,
                            execution_id=execution_id,
                            tool_name=name,
                            logical_key=key,
                            arguments_json=arguments_json,
                            result_json=result,
                        )
                    )
                run_context.evidence_source_ids.update(evidence_ids)
                tools_used.append(result)
                return json.dumps(result)

        def bind(spec):
            async def invoke(ctx, raw_arguments):
                return await tool(spec, raw_arguments)

            return FunctionTool(
                name=spec.name,
                description=spec.description,
                params_json_schema=spec.arguments.model_json_schema(),
                on_invoke_tool=invoke,
            )

        execution_ref = SimpleNamespace(
            id=execution_id,
            tenant_id=tenant_id,
            task_id=task_id,
            attempt=attempt,
            case_id=UUID(context["task"]["case_id"]),
        )
        retrieval = ExecutionRetrieval(
            self.database, self.settings, execution_ref, context, run.retrieved_source_ids
        )
        return [
            bind(spec) for spec in run.adapter.tools if spec.name in definition["tools"]
        ] + retrieval.tools()

    async def _decide(self, run, tools):
        execution_id = run.execution_id
        context, definition, attempt = run.context, run.definition, run.attempt
        mode = run.mode
        usage = {}
        budget = ModelBudget(context, self.settings)
        try:
            holds = stop_contact(context["memory"])
            if holds:
                decision = run.adapter.review("; ".join(i["text"] for i in holds))
            elif context["action_requirements"].get("suspension_reason"):
                decision = run.adapter.review(context["action_requirements"]["suspension_reason"])
            elif definition.get("capability_stage") != "durable_demo":
                decision = run.adapter.review(
                    "This published version is analysis-only; explicitly assign a runnable version."
                )
            else:
                if mode == "openai" and not os.getenv("OPENAI_API_KEY"):
                    raise RuntimeError("AGENT_MODE=openai requires OPENAI_API_KEY")
                model = (
                    definition["model"]
                    if mode == "openai"
                    else run.adapter.scripted_model(context, self.settings.demo_follow_up_seconds)
                )
                agent = Agent(
                    name=definition["name"],
                    instructions=definition["instructions"] + PLATFORM_INSTRUCTIONS,
                    model=model,
                    model_settings=ModelSettings(max_tokens=self.settings.context_output_tokens),
                    tools=tools,
                    output_type=run.adapter.decision_type,
                )
                seconds = (datetime.fromisoformat(context["deadline"]) - now()).total_seconds()
                async with asyncio.timeout(max(0, seconds)):
                    result = await Runner.run(
                        agent,
                        json.dumps(context),
                        max_turns=definition["limits"]["max_model_turns"],
                        run_config=RunConfig(
                            tracing_disabled=True, call_model_input_filter=budget.filter
                        ),
                    )
                decision = result.final_output
                usage = {
                    name: getattr(result.context_wrapper.usage, name)
                    for name in ("requests", "input_tokens", "output_tokens", "total_tokens")
                }
        except ContextBudgetExceeded as exc:
            decision = run.adapter.review(str(exc))
            usage["context_budget_exceeded"] = True
        except StaleExecution:
            decision = None
        except Exception as exc:
            # The SDK wraps callback exceptions in UserError; stale work is still a
            # refresh, not a model/provider failure. Inspect the original cause.
            cause = exc
            seen = set()
            while cause is not None and id(cause) not in seen:
                if isinstance(cause, StaleExecution):
                    return None, usage
                seen.add(id(cause))
                cause = cause.__cause__
            async with self.database.sessions.begin() as session:
                run = await session.get(Execution, execution_id)
                if run.attempt == attempt:
                    run.error_type = type(exc).__name__
            raise
        if budget.calls or usage.get("context_budget_exceeded"):
            usage["context_budget"] = budget.report()
        return decision, usage

    async def _commit(self, run, activation, decision, usage):
        tenant_id, task_id, execution_id = run.tenant_id, run.task_id, run.execution_id
        context, attempt = run.context, run.attempt
        tools_used = run.tools_used
        async with self.database.sessions.begin() as session:
            await policy_lock(session, tenant_id)
            task = await session.scalar(
                select(Task)
                .where(Task.id == task_id, Task.tenant_id == tenant_id)
                .with_for_update()
            )
            execution = await session.get(Execution, execution_id)
            if execution.attempt != attempt:
                return {"status": "CONTINUE"}
            self.adapters.for_execution(task, run.definition, context["domain"]["adapter"])
            memory = await memory_for_task(session, task, adapter=run.adapter)
            if (
                decision is None
                or task.version != context["expected_version"]
                or memory["manifest"]["fingerprint"] != context["memory"]["manifest"]["fingerprint"]
            ):
                execution.status = "STALE"
                execution.outcome_json = {"status": "CONTINUE"}
                execution.finished_at = now()
                return execution.outcome_json
            decision = run.adapter.decision_type.model_validate(decision.model_dump())
            known = (
                {str(r["source_id"]) for r in tools_used}
                | {s["id"] for s in context["sources"]}
                | set(context["memory"]["manifest"]["source_ids"])
                | run.retrieved_source_ids
                | {
                    i["source_id"]
                    for i in context.get("history", {}).get("retrieval", {}).get("results", [])
                }
                | {
                    i["source_id"]
                    for i in (context.get("history", {}).get("summary") or {}).get("entries", [])
                }
            )
            if any(
                str(o.source_id) not in known | run.evidence_source_ids
                for o in decision.observations
            ):
                raise ValueError("Agent cited evidence it was not given")
            step = decision.next_action
            if step.type == "complete" and any(
                i["kind"] == "OBLIGATION" for i in memory["operational_items"]
            ):
                step = NextAction(
                    type="needs_human",
                    follow_up_seconds=None,
                    reason="Resolve open obligations before completing this task.",
                )
                decision.next_action = step
            validation = await run.adapter.validate(
                session, task, context, decision, tools_used, step
            )
            if set(validation.progress) & {
                "memory_items",
                "last_summary",
                "last_reason",
                "last_execution_id",
            }:
                raise ValueError("Domain progress cannot overwrite platform-owned state")
            step = validation.action
            if step.type == "complete" and any(
                i["kind"] == "OBLIGATION" for i in memory["operational_items"]
            ):
                step = NextAction(
                    type="needs_human",
                    follow_up_seconds=None,
                    reason="Resolve open obligations before completing this task.",
                )
                validation.replan_reason = None
            decision.next_action = step
            status = {"wait": "WAITING", "complete": "COMPLETED", "needs_human": "BLOCKED"}[
                step.type
            ]
            reason = step.reason
            if (
                status == "WAITING"
                and activation["cycle"] >= self.settings.demo_max_activations - 1
            ):
                status, reason = (
                    "BLOCKED",
                    "Demo follow-up limit reached; waiting for an operator/provider update.",
                )
            outcome = {"status": status, "reason": reason}
            if status == "WAITING":
                delay = max(
                    1,
                    min(
                        step.follow_up_seconds or self.settings.demo_follow_up_seconds,
                        self.settings.demo_follow_up_seconds,
                    ),
                )
                outcome["wake_at"] = (now() + timedelta(seconds=delay)).isoformat()
            if validation.replan_reason:
                status = "PENDING"
                outcome = {"status": "CONTINUE", "reason": validation.replan_reason}
            task.status = status
            task.version += 1
            task.progress_json = {
                **task.progress_json,
                **validation.progress,
                "last_summary": decision.summary,
                "last_reason": reason,
                "last_execution_id": str(execution_id),
            }
            execution.status, execution.decision_json = (
                "SUCCEEDED",
                decision.model_dump(mode="json"),
            )
            execution.outcome_json, execution.usage_json, execution.finished_at = (
                outcome,
                usage,
                now(),
            )
            for inbox_id in context["inbox_ids"]:
                entry = await session.get(TaskInbox, UUID(inbox_id))
                entry.consumed_by_execution_id = execution_id
            event = ActivityEvent(
                tenant_id=tenant_id,
                case_id=task.case_id,
                task_id=task_id,
                event_type="task.plan_rejected"
                if validation.rejected
                else f"task.{status.lower()}",
                producer="execution",
                dedupe_key=f"execution/{execution_id}",
                actor_ref=f"agent/{task.agent_key}/{task.agent_version}",
                payload={
                    "execution_id": str(execution_id),
                    "summary": decision.summary,
                    "mode": execution.mode,
                    "outcome": outcome,
                },
            )
            session.add(event)
            from agents_platform.check_in_series import notify_series

            await notify_series(session, task, event)
            if validation.replan_reason:
                await session.flush()
                session.add(
                    TaskInbox(
                        tenant_id=tenant_id,
                        case_id=task.case_id,
                        task_id=task_id,
                        event_id=event.id,
                    )
                )
            if status in ("BLOCKED", "FAILED") or (
                status == "COMPLETED" and validation.notify_supervisor
            ):
                from agents_platform.coordination import enqueue_supervisor

                await enqueue_supervisor(session, event)
            return outcome

    @activity.defn
    async def fail_task(self, activation: dict) -> None:
        async with self.database.sessions.begin() as session:
            task = await session.scalar(
                select(Task)
                .where(
                    Task.tenant_id == UUID(activation["tenant_id"]),
                    Task.id == UUID(activation["task_id"]),
                )
                .with_for_update()
            )
            if task and task.status not in TERMINAL:
                task.status, task.version = "FAILED", task.version + 1
                execution = await session.get(
                    Execution, uuid5(task.id, f"execution/{activation['cycle']}")
                )
                if execution:
                    execution.status, execution.finished_at = "FAILED", now()
                event = ActivityEvent(
                    tenant_id=task.tenant_id,
                    case_id=task.case_id,
                    task_id=task.id,
                    event_type="task.failed",
                    producer="workflow",
                    dedupe_key=f"failed/{activation['cycle']}/{task.id}",
                    actor_ref="platform",
                    payload={"reason": "Execution retries exhausted"},
                )
                session.add(event)
                from agents_platform.coordination import enqueue_supervisor

                await enqueue_supervisor(session, event)
                from agents_platform.check_in_series import notify_series

                await notify_series(session, task, event)

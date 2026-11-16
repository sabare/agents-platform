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

from agents import Agent, FunctionTool, RunConfig
from sqlalchemy import select
from temporalio import activity

from agents_platform.audit import (
    append_event,
    authorize_tool,
    bind_audit,
    cached_result,
    make_event,
    reject,
)
from agents_platform.config import Settings, agent_model_settings
from agents_platform.context import (
    PLATFORM_INSTRUCTIONS,
    ContextBudgetExceeded,
    ExecutionRetrieval,
    ModelBudget,
    model_source_content,
)
from agents_platform.db import Database
from agents_platform.demo_policy import model_instructions
from agents_platform.domains import (
    ActionNotDue,
    ActionStateChanged,
    DecisionValidationError,
    DomainAdapter,
    DomainValidation,
    adapter_ref,
    default_registry,
)
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
from agents_platform.observability import (
    bind_identity,
    evaluation_scores,
    observed_tool,
    phase,
    record_context,
    run_agent,
    trace_reference,
)
from agents_platform.task_contracts import NextAction, TaskSnapshot
from agents_platform.telemetry import bind_context, observed_activity

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
    tool_rejections: list[dict] = field(default_factory=list)


class TaskActivities:
    def __init__(self, database: Database, settings: Settings, adapters=None):
        self.database, self.settings = database, settings
        self.adapters = adapters or default_registry()

    @activity.defn
    @observed_activity
    async def execute_task(self, activation: dict) -> dict:
        """The complete activation in three steps, with no database lock during model calls."""
        async with self.database.sessions() as session:
            task = await session.scalar(
                select(Task).where(
                    Task.tenant_id == UUID(activation["tenant_id"]),
                    Task.id == UUID(activation["task_id"]),
                )
            )
            adapter = None
            if task and task.agent_key:
                published = await session.get(AgentVersion, (task.agent_key, task.agent_version))
                if published:
                    adapter = self.adapters.for_execution(task, published.definition_snapshot)
        from agents_platform.communications import process_pending

        await process_pending(
            self.database,
            self.settings,
            UUID(activation["tenant_id"]),
            UUID(activation["task_id"]),
            adapter,
        )
        ingest = getattr(adapter, "ingest_updates", None)
        if ingest:
            await ingest(self.database, self.settings, task.tenant_id, task.id)
        run = await self._prepare(activation)
        if isinstance(run, dict):
            return run  # Previously committed result, closed task, or duplicate signal.
        bind_identity(**{"execution.attempt": run.attempt, "runtime.mode": run.mode})
        bind_audit(actor_ref=f"agent/{run.definition['agent_key']}/{run.definition['version']}")
        decision, usage = await self._decide(run, self._tools(run))
        if decision is not None and usage.get("requests", 0):
            # Preserve the model's choice before backend validation can change it.
            usage["agent_proposed_action"] = decision.next_action.type
        try:
            outcome = await self._commit(run, activation, decision, usage)
            evaluation_scores(outcome, usage)
            return outcome
        except DecisionValidationError as exc:
            # Keep successful logical actions. A fresh activation sees saved state and
            # correction feedback; repeated bad proposals eventually reach human review.
            feedback = str(exc)
            usage = {
                **usage,
                "rejected_decision": decision.model_dump(mode="json"),
                "decision_validation_error": feedback,
            }
            review = run.adapter.review(feedback)
            review.next_action = NextAction(
                type="needs_human", follow_up_seconds=None, reason=feedback
            )
            outcome = await self._commit(run, activation, review, usage, feedback)
            evaluation_scores(outcome, usage)
            return outcome

    @phase("context.assemble")
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
            if activation.get("generation", 0) != task.progress_json.get("recovery_generation", 0):
                return {"status": "CANCELLED", "reason": "Superseded execution generation"}
            if task.status in ("CANCELLED", "FAILED"):
                return {"status": task.status}
            bind_context(
                case_id=task.case_id,
                task_id=task.id,
                workflow_id=task.workflow_id,
                execution_id=execution_id,
            )
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
            advance_human_work = getattr(adapter, "advance_human_work", None)
            if execution is None and advance_human_work:
                human_outcome = await advance_human_work(session, task)
                if human_outcome:
                    return human_outcome
            if execution is None:
                domain = await adapter.load(session, task)
                from agents_platform.communications import blocking_reason

                communication_blocker = await blocking_reason(session, task)
                if domain.evidence_source_ids:
                    scoped_ids = set(
                        await session.scalars(
                            select(Source.id).where(
                                Source.tenant_id == tenant_id,
                                Source.case_id == task.case_id,
                                Source.id.in_(domain.evidence_source_ids),
                            )
                        )
                    )
                    if scoped_ids != set(domain.evidence_source_ids):
                        raise ValueError("Domain context contains evidence outside this case")
                if (
                    domain.ready_at
                    and domain.ready_at > now()
                    and not task.progress_json.get("decision_validation_feedback")
                    and not communication_blocker
                ):
                    if task.status != "WAITING":
                        task.status, task.version = "WAITING", task.version + 1
                        task.progress_json = {
                            **task.progress_json,
                            "last_reason": f"Waiting until {domain.ready_at.isoformat()}.",
                        }
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
                    "observability": trace_reference(),
                    "task": view,
                    "instructions": memory["instructions"],
                    "memory": memory,
                    "history": history,
                    "domain": {
                        "adapter": adapter_ref(adapter),
                        "state": domain.state,
                        "evidence_source_ids": [str(i) for i in domain.evidence_source_ids],
                    },
                    "action_requirements": {
                        **domain.action_requirements,
                        "current_memory_is_authoritative": True,
                        "communication_blocker": communication_blocker,
                        "resolved_items_are_history": True,
                        "decision_validation_feedback": task.progress_json.get(
                            "decision_validation_feedback"
                        ),
                    },
                    "recent_events": [
                        {"type": e.event_type, "payload": e.payload} for e in reversed(recent)
                    ],
                    "new_events": [
                        {"type": e.event_type, "payload": e.payload} for e in unread_events
                    ],
                    "has_new_business_information": any(
                        event.producer not in {"execution", "domain-tool"}
                        for event in unread_events
                    ),
                    "sources": [
                        {"id": str(s.id), "content": model_source_content(s)} for s in sources
                    ],
                    "inbox_ids": [str(i.id) for i in inbox],
                    "expected_version": task.version,
                    "agent_definition_hash": agent.definition_hash,
                    "deadline": (now() + timedelta(minutes=5)).isoformat(),
                }
                record_context(context)
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
                reject("TOOL_NOT_ALLOWED")
                raise ValueError("Tool is not permitted by this agent version")
            if (
                len(tools_used) + len(run_context.tool_rejections)
                >= definition["limits"]["max_tool_calls"]
            ):
                reject("TOOL_BUDGET_EXCEEDED")
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
                    reject("STALE_EXECUTION")
                    raise StaleExecution()
                self.adapters.for_execution(current, definition, context["domain"]["adapter"])
                memory = await memory_for_task(session, current, adapter=run_adapter)
                if (
                    memory["manifest"]["fingerprint"]
                    != context["memory"]["manifest"]["fingerprint"]
                ):
                    reject("STALE_EXECUTION")
                    raise StaleExecution()
                if spec.effect == "contact":
                    from agents_platform.communications import blocking_reason, commitment_due

                    if await blocking_reason(session, current):
                        reject("COMMUNICATION_BLOCKER")
                        raise StaleExecution()
                    promised = await commitment_due(session, current)
                    if promised:
                        reject("COMMITMENT_NOT_DUE")
                        raise ActionNotDue(promised)
                if spec.effect == "contact" and stop_contact(memory):
                    reject("CONTACT_HOLD")
                    raise StaleExecution()
                if current.status != "RUNNING":
                    reject("STALE_EXECUTION")
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
                await authorize_tool(
                    session,
                    policy_version="agent-tool.v1",
                    agent_key=current.agent_key,
                    agent_version=current.agent_version,
                    task_version=current.version,
                    memory_fingerprint=memory["manifest"]["fingerprint"],
                    effect=spec.effect,
                    cached=bool(cached),
                )
                if cached:
                    cached_result()
                    from opentelemetry import trace

                    trace.get_current_span().set_attribute("tool.cached", True)
                    if cached.tool_name != name or cached.arguments_json != arguments_json:
                        reject("IDEMPOTENCY_CONFLICT")
                        raise ValueError(
                            "Logical tool action was already used with different arguments"
                        )
                    result = cached.result_json
                    evidence_ids = result.get("_evidence_source_ids", [])
                else:
                    try:
                        domain_result = await run_adapter.execute_tool(
                            session, current, name, arguments
                        )
                    except ActionNotDue as exc:
                        # Roll back this attempt, not successful tools from earlier
                        # transactions. Do not consume the contact's logical action key.
                        reject("ACTION_NOT_DUE")
                        await session.rollback()
                        rejected = {
                            "tool": name,
                            "status": "NOT_DUE",
                            "action_taken": False,
                            "ready_at": exc.ready_at.isoformat(),
                            "message": "No action taken. Wait until ready_at; keep verified "
                            "information in your final summary.",
                        }
                        run_context.tool_rejections.append(rejected)
                        return json.dumps(rejected)
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
                    if spec.effect == "contact":
                        await append_event(
                            session,
                            make_event(
                                event_type="external_effect.result",
                                action=name,
                                outcome="SUCCEEDED",
                                reason_code="EFFECT_COMMITTED",
                                resource_type="sources",
                                resource_id=str(source.id),
                                details={"simulated": result.get("simulated", False)},
                            ),
                        )
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
                on_invoke_tool=observed_tool(spec.name, invoke),
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
            if context["action_requirements"].get("communication_blocker"):
                decision = run.adapter.review(
                    context["action_requirements"]["communication_blocker"]
                )
            elif holds:
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
                    instructions=model_instructions(
                        definition["instructions"] + PLATFORM_INSTRUCTIONS, self.settings
                    ),
                    model=model,
                    model_settings=agent_model_settings(model, self.settings.context_output_tokens),
                    tools=tools,
                    output_type=run.adapter.decision_type,
                )
                seconds = (datetime.fromisoformat(context["deadline"]) - now()).total_seconds()
                async with asyncio.timeout(max(0, seconds)):
                    result = await run_agent(
                        agent,
                        json.dumps(context),
                        definition=definition,
                        mode=mode,
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
        except ContextBudgetExceeded:
            decision = None
            usage["context_budget_exceeded"] = True
        except StaleExecution:
            decision = None
        except Exception as exc:
            # The SDK wraps callback exceptions in UserError; stale work is still a
            # refresh, not a model/provider failure. Inspect the original cause.
            cause = exc
            seen = set()
            while cause is not None and id(cause) not in seen:
                if isinstance(cause, (StaleExecution, ActionStateChanged)):
                    return None, usage
                if isinstance(cause, ActionNotDue):
                    decision = run.adapter.review(str(cause))
                    decision.next_action = NextAction(
                        type="wait",
                        follow_up_seconds=max(1, int((cause.ready_at - now()).total_seconds()) + 1),
                        reason=str(cause),
                    )
                    return decision, {**usage, "action_rejected": "NOT_DUE"}
                seen.add(id(cause))
                cause = cause.__cause__
            async with self.database.sessions.begin() as session:
                run = await session.get(Execution, execution_id)
                if run.attempt == attempt:
                    run.error_type = type(exc).__name__
            raise
        if budget.calls or usage.get("context_budget_exceeded"):
            usage["context_budget"] = budget.report()
        if run.tool_rejections:
            usage["tool_rejections"] = run.tool_rejections
        return decision, usage

    @phase("result.commit")
    async def _commit(self, run, activation, decision, usage, validation_failure=None):
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
                (decision is None and not usage.get("context_budget_exceeded"))
                or task.version != context["expected_version"]
                or memory["manifest"]["fingerprint"] != context["memory"]["manifest"]["fingerprint"]
            ):
                execution.status = "STALE"
                execution.outcome_json = {"status": "CONTINUE"}
                execution.finished_at = now()
                return execution.outcome_json
            if usage.get("context_budget_exceeded"):
                from agents_platform.recovery import record_context_failure

                return await record_context_failure(session, task, execution, usage)
            decision = run.adapter.decision_type.model_validate(decision.model_dump())
            known = (
                {str(r["source_id"]) for r in tools_used}
                | {s["id"] for s in context["sources"]}
                | set(context["memory"]["manifest"]["source_ids"])
                | run.retrieved_source_ids
                | set(context.get("domain", {}).get("evidence_source_ids", []))
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
                raise DecisionValidationError(
                    "Agent cited evidence it was not given. Use only actual source IDs in the "
                    "context or tool results. Task and requirement IDs are not source IDs."
                )
            step = decision.next_action
            from agents_platform.communications import blocking_reason, commitment_due

            communication_blocker = await blocking_reason(session, task)
            if communication_blocker:
                step = NextAction(
                    type="needs_human", follow_up_seconds=None, reason=communication_blocker
                )
                decision.next_action = step
            if step.type == "complete" and any(
                i["kind"] == "OBLIGATION" for i in memory["operational_items"]
            ):
                step = NextAction(
                    type="needs_human",
                    follow_up_seconds=None,
                    reason="Resolve open obligations before completing this task.",
                )
                decision.next_action = step
            validation_exhausted = False
            if validation_failure:
                failures = task.progress_json.get("decision_validation_replans", 0) + 1
                validation_exhausted = failures > 2
                decision.summary = (
                    "The agent is rechecking its proposal against saved evidence."
                    if failures <= 2
                    else "The agent could not verify its proposal after repeated attempts; "
                    "review the recovery incident to retry or cancel."
                )
                step.reason = decision.summary
                validation = DomainValidation(
                    action=step,
                    progress={
                        "decision_validation_replans": failures,
                        "decision_validation_feedback": validation_failure,
                    },
                    replan_reason=decision.summary if failures <= 2 else None,
                    rejected=True,
                )
            else:
                validation = await run.adapter.validate(
                    session, task, context, decision, tools_used, step
                )
                validation.progress.update(
                    decision_validation_replans=0, decision_validation_feedback=None
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
            if validation_exhausted:
                status = "FAILED"
            reason = step.reason
            # Bound repeated automatic polling, not the business assignment's lifetime.
            # New external/human events can keep a useful task alive for months.
            automatic_activations = (
                0
                if context.get("has_new_business_information", bool(context["inbox_ids"]))
                else task.progress_json.get("automatic_activations", 0) + 1
            )
            if (
                status == "WAITING"
                and not validation.wait_for_event
                and automatic_activations >= self.settings.demo_max_activations
            ):
                status, reason = (
                    "BLOCKED",
                    "Automatic polling limit reached; waiting for a user/provider update.",
                )
            outcome = {"status": status, "reason": reason}
            if status == "WAITING" and validation.wait_for_event:
                outcome["wait_for_event"] = True
            elif status == "WAITING":
                delay = max(
                    1,
                    min(
                        step.follow_up_seconds or self.settings.demo_follow_up_seconds,
                        self.settings.demo_follow_up_seconds,
                    ),
                )
                promised = await commitment_due(session, task)
                outcome["wake_at"] = (promised or (now() + timedelta(seconds=delay))).isoformat()
            if validation.replan_reason:
                status = "PENDING"
                outcome = {"status": "CONTINUE", "reason": validation.replan_reason}
            task.status = status
            task.version += 1
            task.progress_json = {
                **task.progress_json,
                **validation.progress,
                "automatic_activations": automatic_activations,
                **(
                    {"display_title": decision.task_title}
                    if decision.task_title and not task.progress_json.get("display_title")
                    else {}
                ),
                "last_summary": decision.summary,
                "last_reason": reason,
                "last_execution_id": str(execution_id),
            }
            from agents_platform.human_requests import retire_requests

            request_human_help = getattr(run.adapter, "request_human_help", None)
            if status == "BLOCKED":
                from agents_platform.communications import ensure_review

                communication_review = await ensure_review(session, task)
                if not communication_review and request_human_help:
                    await request_human_help(session, task, reason)
            elif status in TERMINAL:
                await retire_requests(
                    session, task, "CANCELLED" if status == "CANCELLED" else "SUPERSEDED"
                )
            execution.status, execution.decision_json = (
                "FAILED" if validation_exhausted else "SUCCEEDED",
                decision.model_dump(mode="json"),
            )
            execution.outcome_json, execution.usage_json, execution.finished_at = (
                outcome,
                usage,
                now(),
            )
            incident_id = None
            if validation_exhausted:
                from agents_platform.recovery import ensure_incident

                execution.error_type = "DecisionValidationError"
                incident = await ensure_incident(
                    session, task, activation["cycle"], "DecisionValidationError"
                )
                incident_id = str(incident.id)
            for inbox_id in context["inbox_ids"]:
                entry = await session.get(TaskInbox, UUID(inbox_id))
                entry.consumed_by_execution_id = execution_id
            event = ActivityEvent(
                tenant_id=tenant_id,
                case_id=task.case_id,
                task_id=task_id,
                event_type="task.failed"
                if validation_exhausted
                else "task.plan_rejected"
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
                    **({"incident_id": incident_id} if incident_id else {}),
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
    @observed_activity
    async def fail_task(self, activation: dict) -> None:
        async with self.database.sessions.begin() as session:
            await policy_lock(session, UUID(activation["tenant_id"]))
            task = await session.scalar(
                select(Task)
                .where(
                    Task.tenant_id == UUID(activation["tenant_id"]),
                    Task.id == UUID(activation["task_id"]),
                )
                .with_for_update()
            )
            if (
                task
                and task.status not in TERMINAL
                and activation.get("generation", 0)
                == task.progress_json.get("recovery_generation", 0)
            ):
                task.status, task.version = "FAILED", task.version + 1
                execution = await session.get(
                    Execution, uuid5(task.id, f"execution/{activation['cycle']}")
                )
                if execution:
                    execution.status, execution.finished_at = "FAILED", now()
                from agents_platform.recovery import ensure_incident

                incident = await ensure_incident(
                    session,
                    task,
                    activation["cycle"],
                    activation.get("failure_type")
                    or (execution.error_type if execution else None)
                    or "ExecutionRetriesExhausted",
                )
                task.progress_json = {
                    **task.progress_json,
                    "last_reason": "Retries exhausted. Resolve the incident to retry or cancel.",
                }
                event = ActivityEvent(
                    tenant_id=task.tenant_id,
                    case_id=task.case_id,
                    task_id=task.id,
                    event_type="task.failed",
                    producer="workflow",
                    dedupe_key=f"failed/{activation['cycle']}/{task.id}",
                    actor_ref="platform",
                    payload={
                        "reason": "Execution retries exhausted",
                        "incident_id": str(incident.id),
                    },
                )
                session.add(event)
                from agents_platform.coordination import enqueue_supervisor

                await enqueue_supervisor(session, event)
                from agents_platform.check_in_series import notify_series

                await notify_series(session, task, event)

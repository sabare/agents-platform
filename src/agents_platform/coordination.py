"""Durable case coordination: saved snapshot → SDK proposal → atomic validated commit."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4, uuid5

from agents import Agent, Model, ModelResponse, RunConfig, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import Field, model_validator
from sqlalchemy import select
from temporalio import activity

from agents_platform.audit import append_event, bind_audit, make_event
from agents_platform.capabilities import discover_capabilities
from agents_platform.client_reviews import review_view
from agents_platform.config import agent_model_settings
from agents_platform.context import (
    PLATFORM_INSTRUCTIONS,
    ContextBudgetExceeded,
    ExecutionRetrieval,
    ModelBudget,
)
from agents_platform.demo_policy import model_instructions
from agents_platform.execution import now
from agents_platform.memory import instructions_for, memory_for_case, policy_lock
from agents_platform.memory_history import historical_context
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    CaseCoordination,
    ClientReview,
    Contact,
    Execution,
    HumanRequest,
    OutboxMessage,
    RecordEvidence,
    Source,
    Task,
    TaskInbox,
    ToolInvocation,
)
from agents_platform.observability import (
    bind_identity,
    evaluation_scores,
    phase,
    record_context,
    run_agent,
    trace_reference,
)
from agents_platform.source_content import model_source_content
from agents_platform.task_contracts import Contract, CreateRecordsTask, RecordRequirementInput
from agents_platform.tasks import TaskError, create_records_task_in_transaction, read_task
from agents_platform.telemetry import bind_context, observed_activity


class RecordsTarget(Contract):
    task_title: str | None = Field(default=None, min_length=1, max_length=160)
    patient_contact_id: UUID
    provider_contact_id: UUID
    requirements: list[RecordRequirementInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_assignment(self):
        CreateRecordsTask(
            case_id=UUID(int=0),
            objective="Validate target",
            **self.model_dump(exclude={"task_title"}),
        )
        return self


class ApprovedAssignment(Contract):
    agent_key: str
    agent_version: int = Field(gt=0)
    objective: str = Field(min_length=1, max_length=2000)
    assignment: dict


class CaseObjective(Contract):
    objective: str = Field(min_length=1, max_length=2000)
    targets: list[RecordsTarget] = Field(default_factory=list, max_length=10)
    approved_assignments: list[ApprovedAssignment] = Field(default_factory=list, max_length=10)
    expected_revision: int | None = Field(default=None, ge=1)


class SupervisorAction(Contract):
    task_title: str | None = Field(default=None, min_length=1, max_length=160)
    type: Literal["create_task", "delegate_task", "request_human_review"]
    agent_key: str | None = None
    agent_version: int | None = None
    task_objective: str | None = Field(default=None, min_length=1, max_length=2000)
    assignment_json: str | None = Field(default=None, max_length=20000)
    review_task_id: UUID | None = Field(
        default=None,
        description=(
            "For request_human_review about a specialist, use that existing child task ID. "
            "Use null only for case-objective clarification. Reuse existing typed human requests."
        ),
    )
    target_index: int | None
    question: str | None


class SupervisorDecision(Contract):
    summary: str = Field(min_length=1, max_length=4000)
    evidence_event_ids: list[UUID] = Field(
        description=(
            "Only new_events[*].id or retrieved event_ids. "
            "Never source_id, task_id or payload IDs; use [] if none."
        )
    )
    actions: list[SupervisorAction] = Field(max_length=3)
    objective_satisfied: bool
    next_review_at: datetime | None


class TargetSupervisorAction(SupervisorAction):
    """Approved records targets only permit indexed creation, never generic delegation."""

    type: Literal["create_task", "request_human_review"]
    agent_key: None = None
    agent_version: None = None
    task_objective: None = None
    assignment_json: None = None


class TargetSupervisorDecision(SupervisorDecision):
    actions: list[TargetSupervisorAction] = Field(max_length=3)


def supervisor_decision_type(context):
    objective = context["objective"]
    if objective.get("targets") and not objective.get("approved_assignments"):
        return TargetSupervisorDecision
    return SupervisorDecision


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def requirements_key(items):
    return sorted(
        json.dumps(
            {
                k: v
                for k, v in r.items()
                if k in ("record_type", "all_dates", "from_date", "through_date")
            },
            sort_keys=True,
        )
        for r in items
    )


def covering_tasks(target, tasks):
    needed = set(requirements_key(target["requirements"]))
    matches = []
    for task in tasks:
        request = task.get("records_request")
        if not request or task["status"] == "CANCELLED":
            continue
        if any(
            str(request[k]) != str(target[k]) for k in ("patient_contact_id", "provider_contact_id")
        ):
            continue
        if needed <= set(requirements_key(request["requirements"])):
            matches.append(task)
    # Prefer verified evidence, then healthy existing work, then a blocker needing review.
    return sorted(
        matches,
        key=lambda t: (
            not verified(t, target),
            t["status"] in ("BLOCKED", "FAILED"),
            t["created_at"],
            t["id"],
        ),
    )


def verified(task, target):
    request = task["records_request"]
    needed = set(requirements_key(target["requirements"]))
    required_ids = {r["id"] for r in request["requirements"] if requirements_key([r])[0] in needed}
    return (
        task["status"] == "COMPLETED"
        and request["status"] == "RECEIVED"
        and bool(required_ids)
        and required_ids <= set(task.get("verified_requirement_ids", []))
    )


async def snapshot(session, coord):
    rows = (
        await session.scalars(
            select(Task)
            .where(
                Task.tenant_id == coord.tenant_id,
                Task.case_id == coord.case_id,
                Task.kind == "AGENT",
            )
            .order_by(Task.created_at, Task.id)
            .limit(201)
        )
    ).all()
    evidence = set(
        (
            await session.scalars(
                select(RecordEvidence.requirement_id).where(
                    RecordEvidence.tenant_id == coord.tenant_id,
                    RecordEvidence.case_id == coord.case_id,
                )
            )
        ).all()
    )
    tasks = []
    for row in rows[:200]:
        view = (await read_task(session, coord.tenant_id, row.id)).model_dump(mode="json")
        view["verified_requirement_ids"] = [
            str(r["id"])
            for r in (view["records_request"] or {}).get("requirements", [])
            if UUID(r["id"]) in evidence
        ]
        tasks.append(view)
    return tasks, len(rows) > 200


async def scoped_instructions(session, coord):
    provider_ids = [UUID(t["provider_contact_id"]) for t in coord.objective_json.get("targets", [])]
    return await instructions_for(session, coord.tenant_id, provider_ids)


async def enqueue_supervisor(session, event, *, force=False):
    """Called inside the business-change transaction. No network I/O here."""
    coord = await session.scalar(
        select(CaseCoordination)
        .where(
            CaseCoordination.tenant_id == event.tenant_id, CaseCoordination.case_id == event.case_id
        )
        .with_for_update()
    )
    if coord is None:
        return False
    await session.flush()
    exists = await session.scalar(
        select(TaskInbox.id).where(
            TaskInbox.tenant_id == event.tenant_id,
            TaskInbox.task_id == coord.group_task_id,
            TaskInbox.event_id == event.id,
        )
    )
    if exists:
        return False
    coord.revision += 1
    if force:
        coord.status = "PENDING"
    session.add(
        TaskInbox(
            tenant_id=coord.tenant_id,
            case_id=coord.case_id,
            task_id=coord.group_task_id,
            event_id=event.id,
        )
    )
    session.add(
        OutboxMessage(
            tenant_id=coord.tenant_id,
            event_id=event.id,
            destination="temporal.wake_case",
            destination_key=coord.workflow_id,
            payload={"tenant_id": str(coord.tenant_id), "case_id": str(coord.case_id)},
        )
    )

    return True


async def submit_objective(session, tenant_id, case_id, body, key):
    async with session.begin():
        return await submit_objective_in_transaction(session, tenant_id, case_id, body, key)


async def submit_objective_in_transaction(session, tenant_id, case_id, body, key):
    case = await session.scalar(
        select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id).with_for_update()
    )
    if case is None:
        raise TaskError(404, "Case not found")
    if case.status != "OPEN":
        raise TaskError(409, "Supervisor requires an open case")
    coord = await session.scalar(
        select(CaseCoordination)
        .where(CaseCoordination.tenant_id == tenant_id, CaseCoordination.case_id == case_id)
        .with_for_update()
    )
    # Keep pre-catalog objective retries compatible when the new optional list is empty.
    request_body = body.model_dump(mode="json")
    if not body.approved_assignments:
        request_body.pop("approved_assignments", None)
    request_hash = digest(request_body)
    dedupe = f"objective/{case_id}/{key}"
    existing = await session.scalar(
        select(ActivityEvent).where(
            ActivityEvent.tenant_id == tenant_id,
            ActivityEvent.producer == "case-operator",
            ActivityEvent.dedupe_key == dedupe,
        )
    )
    if existing:
        if existing.payload["hash"] != request_hash:
            raise TaskError(409, "Idempotency key used for a different objective")
        return coord.group_task_id, False
    if coord and body.expected_revision != coord.revision:
        raise TaskError(409, "Objective revision changed; reload and explicitly replace it")
    if not coord and body.expected_revision is not None:
        raise TaskError(409, "There is no objective to replace")
    if await session.get(AgentVersion, ("case_supervisor", 5)) is None:
        raise TaskError(422, "Publish the supervisor definition with agents-seed first")
    for target in body.targets:
        for contact, roles in (
            (target.patient_contact_id, {"PATIENT", "CLIENT"}),
            (target.provider_contact_id, {"MEDICAL_PROVIDER"}),
        ):
            entry = await session.scalar(
                select(CaseContact).where(
                    CaseContact.tenant_id == tenant_id,
                    CaseContact.case_id == case_id,
                    CaseContact.contact_id == contact,
                )
            )
            if not entry or not roles.intersection(entry.roles):
                raise TaskError(
                    422, "Target participants must have the required roles in this case"
                )
    if coord is None:
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
            status="PENDING",
        )
        session.add(group)
        await session.flush()
        coord = CaseCoordination(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            group_task_id=group.id,
            workflow_id=f"firm/{tenant_id}/case/{case_id}/supervisor",
            objective_json={},
            agent_version=5,
        )
        session.add(coord)
        await session.flush()
    catalog = discover_capabilities()
    cards = await catalog.available(session)
    normalized_assignments = []
    for assignment in body.approved_assignments:
        from agents_platform.delegation import validate_delegation

        try:
            _, normalized = validate_delegation(
                catalog,
                cards,
                SupervisorAction(
                    type="delegate_task",
                    target_index=None,
                    question=None,
                    agent_key=assignment.agent_key,
                    agent_version=assignment.agent_version,
                    task_objective=assignment.objective,
                    assignment_json=json.dumps(assignment.assignment),
                ),
                [],
            )
            normalized_assignments.append(
                {
                    **assignment.model_dump(mode="json"),
                    "assignment": normalized,
                }
            )
        except ValueError as exc:
            raise TaskError(422, str(exc)) from exc
    coord.agent_version = 5
    coord.objective_json = body.model_dump(mode="json", exclude={"expected_revision"})
    coord.objective_json = {
        **coord.objective_json,
        "approved_assignments": normalized_assignments,
    }
    coord.plan_json = {}
    coord.status, coord.stall_count = "PENDING", 0
    from agents_platform.human_requests import retire_requests

    await retire_requests(
        session, await session.get(Task, coord.group_task_id), handler="supervisor_objective.v1"
    )
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        task_id=coord.group_task_id,
        event_type="case.objective_submitted",
        producer="case-operator",
        dedupe_key=dedupe,
        actor_ref="local-operator",
        payload={
            "objective": body.objective,
            "hash": request_hash,
            "targets": len(body.targets),
        },
    )
    session.add(event)
    await enqueue_supervisor(session, event, force=True)
    return coord.group_task_id, True


class SupervisorActivities:
    def __init__(self, database, settings, catalog=None):
        self.database, self.settings = database, settings
        self.catalog = catalog or discover_capabilities()

    @activity.defn
    @observed_activity
    async def coordinate_case(self, activation: dict):
        # Calendar cadence is operator-approved. Its deterministic delegation belongs to
        # case coordination; published records-only model definitions remain unchanged.
        from agents_platform.monthly_check_ins import delegate_due_monthly

        await delegate_due_monthly(
            self.database, UUID(activation["tenant_id"]), UUID(activation["case_id"])
        )
        prepared = await self._prepare(activation)
        if isinstance(prepared, dict):
            return prepared
        run, definition = prepared
        bind_audit(actor_ref=f"agent/{definition['agent_key']}/{definition['version']}")
        bind_identity(**{"execution.attempt": run.attempt, "runtime.mode": run.mode})
        try:
            decision, usage = await self._decide(run, definition)
        except Exception as exc:
            async with self.database.sessions.begin() as session:
                saved = await session.get(Execution, run.id, with_for_update=True)
                if saved and saved.attempt == run.attempt and saved.outcome_json is None:
                    saved.error_type = type(exc).__name__
            raise
        outcome = await self._commit(run, decision, usage)
        evaluation_scores(outcome, usage)
        return outcome

    @phase("context.assemble")
    async def _prepare(self, activation):
        tenant, case_id = UUID(activation["tenant_id"]), UUID(activation["case_id"])
        async with self.database.sessions.begin() as session:
            await policy_lock(session, tenant)
            coord = await session.scalar(
                select(CaseCoordination)
                .where(CaseCoordination.tenant_id == tenant, CaseCoordination.case_id == case_id)
                .with_for_update()
            )
            if coord is None:
                return {"status": "CLOSED"}
            case = await session.get(Case, case_id)
            if case.status != "OPEN":
                return {"status": "CLOSED"}
            run_id = uuid5(coord.group_task_id, f"supervisor/{activation['cycle']}")
            bind_context(
                case_id=case_id,
                task_id=coord.group_task_id,
                execution_id=run_id,
                workflow_id=coord.workflow_id,
            )
            run = await session.get(Execution, run_id)
            if run and run.outcome_json is not None:
                return run.outcome_json
            if coord.status == "FAILED":
                return {"status": "WAITING"}
            if run:
                if "memory" not in run.context_json:
                    run.status, run.finished_at, run.outcome_json = (
                        "STALE",
                        now(),
                        {"status": "CONTINUE"},
                    )
                    return run.outcome_json
                if coord.agent_version >= 3 and run.context_json.get("capabilities") != (
                    await self.catalog.available(session)
                ):
                    run.status, run.finished_at, run.outcome_json = (
                        "STALE",
                        now(),
                        {"status": "CONTINUE"},
                    )
                    return run.outcome_json
                if datetime.fromisoformat(run.context_json["deadline"]) <= now():
                    run.status, run.finished_at, run.outcome_json = (
                        "STALE",
                        now(),
                        {
                            "status": "CONTINUE",
                            "reason": "Previous turn timed out; prepare a fresh bounded turn.",
                        },
                    )
                    return run.outcome_json
                run.attempt += 1
                return run, (
                    await session.get(AgentVersion, ("case_supervisor", coord.agent_version))
                ).definition_snapshot
            inbox = (
                await session.scalars(
                    select(TaskInbox)
                    .where(
                        TaskInbox.tenant_id == tenant,
                        TaskInbox.task_id == coord.group_task_id,
                        TaskInbox.consumed_by_execution_id.is_(None),
                    )
                    .order_by(TaskInbox.created_at)
                    .limit(100)
                )
            ).all()
            if not inbox and activation.get("trigger") not in ("timer", "continue"):
                outcome = {"status": "WAITING"}
                if coord.plan_json.get("next_review_at"):
                    outcome["wake_at"] = coord.plan_json["next_review_at"]
                return outcome
            tasks, truncated = await snapshot(session, coord)
            events = (
                await session.scalars(
                    select(ActivityEvent).where(
                        ActivityEvent.tenant_id == tenant,
                        ActivityEvent.id.in_([i.event_id for i in inbox]),
                    )
                )
            ).all()
            source_ids = [
                UUID(e.payload["source_id"]) for e in events if e.payload.get("source_id")
            ]
            sources = (
                await session.scalars(
                    select(Source).where(
                        Source.tenant_id == tenant,
                        Source.case_id == case_id,
                        Source.id.in_(source_ids),
                    )
                )
            ).all()
            memory = await memory_for_case(session, coord)
            definition = await session.get(AgentVersion, ("case_supervisor", coord.agent_version))
            context = {
                "observability": trace_reference(),
                "objective": coord.objective_json,
                "plan": coord.plan_json,
                "revision": coord.revision,
                "case_id": str(case_id),
                "tasks": tasks,
                "tasks_truncated": truncated,
                "new_events": [
                    {"id": str(e.id), "type": e.event_type, "payload": e.payload} for e in events
                ],
                "sources": [
                    {"id": str(s.id), "kind": s.kind, "content": model_source_content(s)}
                    for s in sources
                ],
                "human_requests": [
                    {
                        "id": str(h.id),
                        "task_id": str(h.task_id),
                        "handler": h.handler,
                        "question": h.question,
                        "status": h.status,
                    }
                    for h in (
                        await session.scalars(
                            select(HumanRequest)
                            .where(
                                HumanRequest.tenant_id == tenant,
                                HumanRequest.case_id == case_id,
                                HumanRequest.status == "OPEN",
                            )
                            .limit(100)
                        )
                    ).all()
                ],
                "instructions": memory["instructions"],
                "memory": memory,
                "history": await historical_context(
                    session, tenant, case_id, coord.objective_json.get("objective", "case history")
                ),
                "capabilities": await self.catalog.available(session),
                "contacts": [
                    {"id": str(contact.id), "name": contact.display_name, "roles": link.roles}
                    for contact, link in (
                        await session.execute(
                            select(Contact, CaseContact)
                            .join(
                                CaseContact,
                                (Contact.id == CaseContact.contact_id)
                                & (Contact.tenant_id == CaseContact.tenant_id),
                            )
                            .where(CaseContact.tenant_id == tenant, CaseContact.case_id == case_id)
                            .order_by(Contact.id)
                            .limit(201)
                        )
                    ).all()
                ],
                "agent_definition_hash": definition.definition_hash,
                "inbox_ids": [str(i.id) for i in inbox],
                "deadline": (now() + timedelta(minutes=2)).isoformat(),
            }
            record_context(context)
            run = Execution(
                id=run_id,
                tenant_id=tenant,
                case_id=case_id,
                task_id=coord.group_task_id,
                cycle=activation["cycle"],
                mode=self.settings.agent_mode,
                status="RUNNING",
                context_json=context,
            )
            session.add(run)
            await session.flush()
            return run, definition.definition_snapshot

    async def _decide(self, run, definition):
        context = run.context_json
        if context["objective"].get("monitor_client_updates") and not context["objective"].get(
            "targets"
        ):
            updates = [e for e in context["new_events"] if e["type"] == "client.meaningful_update"]
            return SupervisorDecision(
                summary=(
                    f"Monitoring client self-reports; {len(updates)} new meaningful updates, "
                    f"{sum(e['type'] == 'client.review_decided' for e in context['new_events'])} "
                    "human decisions received."
                ),
                evidence_event_ids=[UUID(e["id"]) for e in updates],
                actions=[],
                objective_satisfied=False,
                next_review_at=None,
            ), {}
        if context["plan"].get("automatic_decisions", 0) >= 30 and not context["new_events"]:
            return SupervisorDecision(
                summary="Automatic decision budget reached.",
                evidence_event_ids=[],
                actions=[
                    SupervisorAction(
                        type="request_human_review",
                        target_index=None,
                        question="Review the case objective and explicitly revise it to continue.",
                    )
                ],
                objective_satisfied=False,
                next_review_at=None,
            ), {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        model = SupervisorScriptedModel(context) if run.mode == "scripted" else definition["model"]
        retrieval = ExecutionRetrieval(self.database, self.settings, run, context)
        budget = ModelBudget(context, self.settings)
        from agents_platform.delegation import delegation_tools, supervisor_delegation_cards

        proposals = []
        specialist_tools = (
            delegation_tools(
                self.catalog, supervisor_delegation_cards(context), proposals, SupervisorAction
            )
            if definition["version"] >= 3
            else []
        )
        agent = Agent(
            name=definition["name"],
            model=model,
            instructions=model_instructions(
                definition["instructions"] + PLATFORM_INSTRUCTIONS, self.settings
            ),
            tools=[*retrieval.tools(), *specialist_tools],
            model_settings=agent_model_settings(model, self.settings.context_output_tokens),
            output_type=supervisor_decision_type(context),
        )
        seconds = max(0.1, (datetime.fromisoformat(context["deadline"]) - now()).total_seconds())
        try:
            async with asyncio.timeout(seconds):
                result = await run_agent(
                    agent,
                    json.dumps(context, default=str),
                    definition=definition,
                    mode=run.mode,
                    max_turns=8,
                    run_config=RunConfig(
                        tracing_disabled=True, call_model_input_filter=budget.filter
                    ),
                )
        except ContextBudgetExceeded as exc:
            return SupervisorDecision(
                summary="Case context needs operator review.",
                evidence_event_ids=[],
                actions=[
                    SupervisorAction(
                        type="request_human_review", target_index=None, question=str(exc)
                    )
                ],
                objective_satisfied=False,
                next_review_at=None,
            ), {"context_budget_exceeded": True, "context_budget": budget.report()}
        decision = result.final_output
        for proposal in proposals:
            if proposal not in decision.actions:
                decision.actions.append(proposal)
        if len(decision.actions) > 3:
            raise ValueError("Supervisor exceeded the three-action budget")
        usage = result.context_wrapper.usage
        return decision, {
            **{
                k: getattr(usage, k)
                for k in ("requests", "input_tokens", "output_tokens", "total_tokens")
            },
            "context_budget": budget.report(),
        }

    @phase("result.commit")
    async def _commit(self, prepared, decision, usage):
        context = prepared.context_json
        async with self.database.sessions.begin() as session:
            await policy_lock(session, prepared.tenant_id)
            case = await session.scalar(
                select(Case)
                .where(Case.tenant_id == prepared.tenant_id, Case.id == prepared.case_id)
                .with_for_update()
            )
            coord = await session.scalar(
                select(CaseCoordination)
                .where(
                    CaseCoordination.tenant_id == prepared.tenant_id,
                    CaseCoordination.case_id == prepared.case_id,
                )
                .with_for_update()
            )
            run = await session.get(Execution, prepared.id, with_for_update=True)
            if run.outcome_json is not None:
                return run.outcome_json
            if run.attempt != prepared.attempt:
                return {"status": "CONTINUE"}
            tasks, truncated = await snapshot(session, coord)
            memory = await memory_for_case(session, coord)

            def versions(rows):
                return {t["id"]: t["version"] for t in rows}

            if (
                case.status != "OPEN"
                or coord.revision != context["revision"]
                or versions(tasks) != versions(context["tasks"])
                or memory["manifest"]["fingerprint"] != context["memory"]["manifest"]["fingerprint"]
            ):
                run.status, run.finished_at, run.outcome_json = (
                    "STALE",
                    now(),
                    {"status": "CONTINUE"},
                )
                return run.outcome_json
            targets = coord.objective_json.get("targets", [])
            client_reviews = (
                await session.scalars(
                    select(ClientReview)
                    .where(
                        ClientReview.tenant_id == coord.tenant_id,
                        ClientReview.case_id == coord.case_id,
                    )
                    .order_by(ClientReview.created_at, ClientReview.id)
                )
            ).all()
            client_review_tasks = {str(r.task_id) for r in client_reviews}
            # Rebuild only client-report reviews from authoritative decision state.
            # Leave records/objective reviews untouched, including other blockers on the same case.
            reviews = [
                r
                for r in coord.plan_json.get("reviews", [])
                if r.get("kind") != "CLIENT_REPORT"
                and not (
                    r.get("task_id") in client_review_tasks
                    and r.get("question", "").startswith("Client self-report")
                )
            ]
            applied, rejected = [], []
            if usage.get("context_budget_exceeded"):
                decision.actions = []
                rejected.append("Required supervisor context exceeds its budget")
            event_ids = {e["id"] for e in context["new_events"]}
            retrieval_calls = await session.scalars(
                select(ToolInvocation).where(
                    ToolInvocation.tenant_id == prepared.tenant_id,
                    ToolInvocation.execution_id == prepared.id,
                    ToolInvocation.tool_name.in_(["retrieve_context", "read_evidence"]),
                )
            )
            for call in retrieval_calls:
                for item in [call.result_json, *call.result_json.get("results", [])]:
                    event_ids.update(item.get("event_ids", []))
            if not {str(i) for i in decision.evidence_event_ids} <= event_ids:
                rejected.append("Decision cites an event outside its saved context")

            def review(question, task_id=None, *, kind="UNRESOLVED"):
                key = digest(
                    {
                        "case": str(coord.case_id),
                        "task": task_id,
                        "kind": "unresolved_review" if kind == "UNRESOLVED" else kind,
                    }
                )
                if not any(r["key"] == key for r in reviews):
                    reviews.append(
                        {
                            "key": key,
                            "task_id": task_id,
                            "question": question,
                            "kind": kind,
                            "status": "UNRESOLVED",
                        }
                    )

            client_updates = list(coord.plan_json.get("client_updates", []))
            known_reports = {u["report_id"] for u in client_updates}
            for event in context["new_events"]:
                if event["type"] == "client.meaningful_update":
                    update = event["payload"]
                    if update["report_id"] not in known_reports:
                        client_updates.append(update)
                        known_reports.add(update["report_id"])
            reviews.extend(
                {
                    "key": f"client-review/{r.id}",
                    "kind": "CLIENT_REPORT",
                    "review_id": str(r.id),
                    "report_id": str(r.report_id),
                    "task_id": str(r.task_id),
                    "question": r.question,
                    "status": "UNRESOLVED",
                }
                for r in client_reviews
                if r.status == "OPEN"
            )
            review_by_report = {str(r.report_id): review_view(r) for r in client_reviews}
            task_statuses = {t["id"]: t["status"] for t in tasks}
            client_updates = [
                {
                    **u,
                    "human_review": review_by_report.get(u["report_id"]),
                    "task_status": task_statuses.get(u["task_id"]),
                }
                for u in client_updates
            ]
            generic_mode = coord.agent_version >= 3 and (
                not targets or bool(coord.objective_json.get("approved_assignments"))
            )
            if truncated or (
                not generic_mode
                and not targets
                and not coord.objective_json.get("monitor_client_updates")
            ):
                review("Clarify the records targets; a complete task overview is required.")
            if any(
                a.type == "create_task"
                and (a.target_index is None or not 0 <= a.target_index < len(targets))
                for a in decision.actions
            ):
                rejected.append("Assignment refers to an unapproved target")
            from agents_platform.delegation import apply_delegation, validate_delegation

            validated_delegations = {}
            for index, action in enumerate(decision.actions):
                if action.type != "delegate_task":
                    continue
                try:
                    if coord.agent_version < 3:
                        raise ValueError(
                            "This supervisor version does not support generic delegation"
                        )
                    if targets and action.agent_key == "medical_records_collector":
                        raise ValueError("Use approved target indices for this records objective")
                    card, payload = validate_delegation(
                        self.catalog,
                        context.get("capabilities", []),
                        action,
                        coord.objective_json.get("approved_assignments", []),
                    )
                    if card not in await self.catalog.available(session):
                        raise ValueError("Capability is no longer published or exposed")
                    validated_delegations[index] = (card, payload)
                except ValueError as exc:
                    rejected.append(str(exc))
            # Re-check participants before applying any action in the batch.
            for action in decision.actions:
                if (
                    action.type != "create_task"
                    or action.target_index is None
                    or not 0 <= action.target_index < len(targets)
                ):
                    continue
                target = targets[action.target_index]
                for contact, roles in (
                    (target["patient_contact_id"], {"PATIENT", "CLIENT"}),
                    (target["provider_contact_id"], {"MEDICAL_PROVIDER"}),
                ):
                    participant = await session.scalar(
                        select(CaseContact).where(
                            CaseContact.tenant_id == coord.tenant_id,
                            CaseContact.case_id == coord.case_id,
                            CaseContact.contact_id == UUID(contact),
                        )
                    )
                    if not participant or not roles.intersection(participant.roles):
                        rejected.append(
                            "Approved participant roles changed; operator review required"
                        )
            for action in decision.actions:
                if (
                    action.type == "request_human_review"
                    and action.review_task_id
                    and not any(t["id"] == str(action.review_task_id) for t in tasks)
                ):
                    rejected.append("Review refers to a task outside this case")
            if not rejected and not truncated:
                for index, action in enumerate(decision.actions):
                    if action.type == "delegate_task":
                        from fastapi import HTTPException

                        card, payload = validated_delegations[index]
                        try:
                            # Domain rejection rolls back its business rows/outbox as one unit.
                            async with session.begin_nested():
                                task_row, created = await apply_delegation(
                                    session, self.catalog, coord, action, card, payload
                                )
                                task = (
                                    await read_task(session, coord.tenant_id, task_row.id)
                                ).model_dump(mode="json")
                            if not any(t["id"] == task["id"] for t in tasks):
                                tasks.append(task)
                            applied.append(
                                {
                                    "type": "create_task" if created else "reuse_task",
                                    "task_id": task["id"],
                                    "agent_key": card["agent_key"],
                                }
                            )
                        except (TaskError, HTTPException, ValueError) as exc:
                            review(str(getattr(exc, "detail", exc)))
                        continue
                    if action.type == "request_human_review":
                        linked = str(action.review_task_id) if action.review_task_id else None
                        review(action.question or "Clarify the next permitted step.", linked)
                        continue
                    if action.target_index is None or not 0 <= action.target_index < len(targets):
                        rejected.append("Assignment refers to an unapproved target")
                        continue
                    target = targets[action.target_index]
                    matches = covering_tasks(target, tasks)
                    if matches:
                        applied.append(
                            {
                                "type": "reuse_task",
                                "task_id": matches[0]["id"],
                                "target_index": action.target_index,
                            }
                        )
                        continue
                    # An overlapping request must not result in duplicate outreach.
                    overlaps = [
                        t
                        for t in tasks
                        if t.get("records_request")
                        and t["status"] != "CANCELLED"
                        and all(
                            t["records_request"][k] == target[k]
                            for k in ("patient_contact_id", "provider_contact_id")
                        )
                    ]
                    if overlaps:
                        review(
                            "Review this overlapping request before assigning more outreach.",
                            overlaps[0]["id"],
                        )
                        continue
                    cancelled = [
                        {**t, "status": "PENDING"} for t in tasks if t["status"] == "CANCELLED"
                    ]
                    if covering_tasks(target, cancelled):
                        review("Cancelled scope requires an explicit repeat policy.")
                        continue
                    provider = await session.get(Contact, UUID(target["provider_contact_id"]))
                    record_names = ", ".join(
                        r["record_type"].lower() for r in target["requirements"]
                    )
                    assignment = CreateRecordsTask(
                        case_id=coord.case_id,
                        parent_task_id=coord.group_task_id,
                        agent_version=5
                        if coord.agent_version >= 5
                        else 4
                        if coord.agent_version >= 4
                        else 3
                        if coord.agent_version >= 2
                        else 2,
                        objective=f"Collect {record_names} from {provider.display_name}",
                        **{k: v for k, v in target.items() if k != "task_title"},
                    )
                    view, created = await create_records_task_in_transaction(
                        session,
                        coord.tenant_id,
                        assignment,
                        f"supervisor/{coord.case_id}/{digest(target)}",
                        actor_ref=f"agent/case_supervisor/{coord.agent_version}",
                    )
                    task = view.model_dump(mode="json")
                    task["verified_requirement_ids"] = []
                    tasks.append(task)
                    title = action.task_title or target.get("task_title")
                    if created and title:
                        assigned = await session.get(Task, view.id)
                        assigned.progress_json = {**assigned.progress_json, "display_title": title}
                    applied.append(
                        {
                            "type": "create_task" if created else "reuse_task",
                            "task_id": str(view.id),
                            "target_index": action.target_index,
                        }
                    )
            for target in targets:
                matches = covering_tasks(target, tasks)
                if matches and matches[0]["status"] in ("BLOCKED", "FAILED"):
                    review(
                        matches[0]["progress_json"].get(
                            "last_reason", "Specialist needs operator review."
                        ),
                        matches[0]["id"],
                    )
                if (
                    matches
                    and matches[0]["status"] == "COMPLETED"
                    and not verified(matches[0], target)
                ):
                    review(
                        "A completed task lacks verified evidence for this scope.", matches[0]["id"]
                    )
            await append_event(
                session,
                make_event(
                    event_type="authorization.decision",
                    action="supervisor.apply_proposals",
                    outcome="DENIED" if rejected else "ALLOWED",
                    reason_code="PROPOSAL_REJECTED" if rejected else "POLICY_PASSED",
                    policy_version="supervisor-proposals.v1",
                    details={
                        "rejection_count": len(rejected),
                        "objective_revision": coord.revision,
                        "memory_fingerprint": memory["manifest"]["fingerprint"],
                    },
                ),
            )
            technical_failure = bool(usage.get("context_budget_exceeded"))
            citation_rejected = rejected == ["Decision cites an event outside its saved context"]
            citation_replans = coord.plan_json.get("citation_replans", 0) + 1
            if citation_rejected:
                usage = {**usage, "decision_validation_error": rejected[0]}
                if citation_replans <= 2:
                    coord.plan_json = {
                        **coord.plan_json,
                        "citation_replans": citation_replans,
                        "correction_feedback": "The previous proposal was rejected; no actions "
                        "were applied. evidence_event_ids must use ONLY new_events[*].id or "
                        "event_ids explicitly returned by retrieval tools. Never use source, "
                        "task or requirement IDs. Use [] if no valid event citation is needed. "
                        "Recheck current state and submit a corrected proposal.",
                        "rejected": rejected,
                        "decision_count": coord.plan_json.get("decision_count", 0) + 1,
                        "automatic_decisions": coord.plan_json.get("automatic_decisions", 0) + 1,
                        "last_execution_id": str(run.id),
                    }
                    coord.status, coord.revision = "PENDING", coord.revision + 1
                    run.status, run.finished_at = "SUCCEEDED", now()
                    run.decision_json, run.usage_json = decision.model_dump(mode="json"), usage
                    run.outcome_json = {
                        "status": "CONTINUE",
                        "reason": "Correcting an invalid citation",
                    }
                    session.add(
                        ActivityEvent(
                            tenant_id=coord.tenant_id,
                            case_id=coord.case_id,
                            task_id=coord.group_task_id,
                            event_type="task.plan_rejected",
                            producer="supervisor",
                            dedupe_key=f"correction/{run.id}",
                            actor_ref="platform",
                            payload={
                                "execution_id": str(run.id),
                                "summary": "The supervisor is correcting an unsupported citation. "
                                "No rejected actions were applied.",
                                "attempt": citation_replans,
                            },
                        )
                    )
                    return run.outcome_json
                technical_failure = True
            if rejected:
                if technical_failure:
                    review(
                        "The supervisor needs technical recovery: required context did not fit."
                        if usage.get("context_budget_exceeded")
                        else "The supervisor could not correct its citations after two retries.",
                        kind="TECHNICAL",
                    )
                else:
                    coord.stall_count += 1
                    review("Supervisor proposal rejected: " + "; ".join(rejected))
            all_covered = (
                bool(targets)
                and not truncated
                and all(
                    any(verified(t, target) for t in covering_tasks(target, tasks))
                    for target in targets
                )
            )
            delegated = [
                t
                for t in tasks
                if t["parent_task_id"] == str(coord.group_task_id)
                and t["progress_json"].get("supervisor_delegation")
            ]
            if generic_mode:
                completed = set(
                    (
                        await session.scalars(
                            select(Execution.task_id).where(
                                Execution.tenant_id == coord.tenant_id,
                                Execution.case_id == coord.case_id,
                                Execution.task_id.in_([UUID(t["id"]) for t in delegated]),
                                Execution.status == "SUCCEEDED",
                                Execution.outcome_json["status"].as_string() == "COMPLETED",
                            )
                        )
                    ).all()
                )
                approved = coord.objective_json.get("approved_assignments", [])
                approved_covered = all(
                    any(
                        t["agent_key"] == a["agent_key"]
                        and t["agent_version"] == a["agent_version"]
                        and t["progress_json"]["supervisor_delegation"]["assignment"]
                        == a["assignment"]
                        for t in delegated
                    )
                    for a in approved
                )
                records_covered = all_covered if targets else True
                all_covered = (
                    records_covered
                    and bool(delegated)
                    and approved_covered
                    and not truncated
                    and all(
                        t["status"] == "COMPLETED" and UUID(t["id"]) in completed for t in delegated
                    )
                )
                for task in delegated:
                    if task["status"] in ("BLOCKED", "FAILED", "CANCELLED"):
                        review(
                            task["progress_json"].get("last_reason", "Specialist needs review."),
                            task["id"],
                        )
                if (
                    not delegated
                    and not applied
                    and not reviews
                    and not coord.objective_json.get("monitor_client_updates")
                ):
                    review(
                        "No specialist assignment covers the objective; clarify required inputs."
                    )
            if all_covered:
                # This review describes a missing fact, not a human authorization.
                # Verified current coverage resolves it; consent reviews stay separate.
                reviews = [r for r in reviews if r.get("kind") != "EVIDENCE_PENDING"]
            if decision.objective_satisfied and not all_covered:
                unfinished = any(
                    t["status"] != "COMPLETED" for t in (delegated if generic_mode else tasks)
                )
                review(
                    "Completion proposed without verified evidence for every approved target.",
                    kind="EVIDENCE_PENDING" if unfinished else "UNRESOLVED",
                )
            if coord.stall_count >= 2:
                review("Repeated invalid decisions; explicit operator revision is required.")
            uncovered = (
                (not approved_covered or any(not covering_tasks(t, tasks) for t in targets))
                if generic_mode
                else any(not covering_tasks(t, tasks) for t in targets)
            )
            if not reviews and uncovered and not applied:
                review("No assignment covers the approved scope; clarify the next action.")
            if (
                all_covered
                and not technical_failure
                and not decision.objective_satisfied
                and not decision.actions
            ):
                review(
                    "Scope is verified but the objective is not confirmed. Clarify remaining work."
                )
            status = (
                "NEEDS_REVIEW"
                if reviews
                else "SATISFIED"
                if all_covered and decision.objective_satisfied
                else "MONITORING"
            )
            next_review = decision.next_review_at
            if next_review and next_review.tzinfo is None:
                next_review = None
            if next_review:
                next_review = max(
                    now() + timedelta(minutes=1), min(next_review, now() + timedelta(days=7))
                )
            outcome = {"status": "WAITING", "coordination_status": status}
            if next_review and status == "MONITORING":
                outcome["wake_at"] = next_review.isoformat()
            plan = {
                "client_updates": client_updates[-20:],
                "client_review_outcomes": [
                    {**review_view(r), "task_status": task_statuses.get(str(r.task_id))}
                    for r in client_reviews[-20:]
                ],
                "decision_count": coord.plan_json.get("decision_count", 0) + 1,
                "automatic_decisions": 0
                if context["new_events"]
                else coord.plan_json.get("automatic_decisions", 0) + 1,
                "citation_replans": citation_replans if citation_rejected else 0,
                "next_review_at": outcome.get("wake_at"),
                "summary": decision.summary,
                "reviews": reviews,
                "actions": applied,
                "rejected": rejected,
                "target_task_ids": [
                    [t["id"] for t in covering_tasks(target, tasks)] for target in targets
                ],
                "delegated_task_ids": [t["id"] for t in delegated],
                "last_execution_id": str(run.id),
            }
            coord.plan_json, coord.status = plan, status
            from agents_platform.human_request_handlers import sync_supervisor_request

            await sync_supervisor_request(session, coord)
            coord.revision += 1
            run.status, run.decision_json, run.outcome_json, run.usage_json, run.finished_at = (
                "SUCCEEDED",
                decision.model_dump(mode="json"),
                outcome,
                usage,
                now(),
            )
            if technical_failure:
                run.status = "FAILED"
                run.error_type = (
                    "ContextBudgetExceeded"
                    if usage.get("context_budget_exceeded")
                    else "DecisionValidationError"
                )
            for inbox_id in context["inbox_ids"]:
                entry = await session.get(TaskInbox, UUID(inbox_id))
                entry.consumed_by_execution_id = run.id
            event = ActivityEvent(
                id=uuid4(),
                tenant_id=coord.tenant_id,
                case_id=coord.case_id,
                task_id=coord.group_task_id,
                event_type="supervisor.review_completed",
                producer="supervisor",
                dedupe_key=f"review/{run.id}",
                actor_ref=f"agent/case_supervisor/{coord.agent_version}",
                payload={
                    "summary": decision.summary,
                    "status": status,
                    "execution_id": str(run.id),
                    "actions": applied,
                    "reviews": reviews,
                    "rejected": rejected,
                },
            )
            session.add(event)
            remaining = await session.scalar(
                select(TaskInbox.id)
                .where(
                    TaskInbox.tenant_id == coord.tenant_id,
                    TaskInbox.task_id == coord.group_task_id,
                    TaskInbox.consumed_by_execution_id.is_(None),
                )
                .limit(1)
            )
            if remaining or (not reviews and uncovered and applied):
                outcome["status"] = "CONTINUE"
                run.outcome_json = dict(outcome)
            return outcome

    @activity.defn
    @observed_activity
    async def fail_supervisor(self, activation):
        async with self.database.sessions.begin() as session:
            coord = await session.scalar(
                select(CaseCoordination)
                .where(
                    CaseCoordination.tenant_id == UUID(activation["tenant_id"]),
                    CaseCoordination.case_id == UUID(activation["case_id"]),
                )
                .with_for_update()
            )
            if not coord:
                return
            run = await session.get(
                Execution, uuid5(coord.group_task_id, f"supervisor/{activation['cycle']}")
            )
            if run and (run.outcome_json is not None or run.status == "FAILED"):
                return
            if run and coord.revision != run.context_json["revision"]:
                run.status, run.finished_at = "STALE", now()
                return
            coord.status = "FAILED"
            coord.plan_json = {
                **coord.plan_json,
                "summary": "Supervisor retries exhausted. Specialists continue independently. "
                "Explicitly revise the objective to retry.",
            }
            session.add(
                ActivityEvent(
                    tenant_id=coord.tenant_id,
                    case_id=coord.case_id,
                    task_id=coord.group_task_id,
                    event_type="supervisor.failed",
                    producer="supervisor",
                    dedupe_key=f"supervisor-failed/{activation['cycle']}/{coord.id}",
                    actor_ref="platform",
                    payload={"summary": coord.plan_json["summary"]},
                )
            )
            if run:
                run.status, run.finished_at, run.error_type = (
                    "FAILED",
                    now(),
                    run.error_type or "ActivityRetriesExhausted",
                )


class SupervisorScriptedModel(Model):
    """Offline test model; the same SDK and commit path as real OpenAI calls."""

    def __init__(self, context):
        self.context = context

    async def get_response(self, *args, **kwargs):
        ctx = self.context
        targets, tasks = ctx["objective"].get("targets", []), ctx["tasks"]
        actions = []
        for index, target in enumerate(targets):
            if not covering_tasks(target, tasks):
                actions.append(
                    SupervisorAction(type="create_task", target_index=index, question=None)
                )
        approved = ctx["objective"].get("approved_assignments", [])
        delegated = [t for t in tasks if t["progress_json"].get("supervisor_delegation")]
        for assignment in approved:
            if not any(
                t["agent_key"] == assignment["agent_key"]
                and t["agent_version"] == assignment["agent_version"]
                and t["progress_json"]["supervisor_delegation"]["assignment"]
                == assignment["assignment"]
                for t in delegated
            ):
                actions.append(
                    SupervisorAction(
                        type="delegate_task",
                        target_index=None,
                        question=None,
                        agent_key=assignment["agent_key"],
                        agent_version=assignment["agent_version"],
                        task_objective=assignment["objective"],
                        assignment_json=json.dumps(assignment["assignment"]),
                    )
                )
        if (not targets and not approved and not delegated) or ctx["tasks_truncated"]:
            actions = [
                SupervisorAction(
                    type="request_human_review",
                    target_index=None,
                    question="Provide explicit records targets or narrow the case scope.",
                )
            ]
        complete = bool(targets) and all(
            any(verified(t, target) for t in covering_tasks(target, tasks)) for target in targets
        )
        if delegated:
            complete = (
                (complete if targets else True)
                and all(t["status"] == "COMPLETED" for t in delegated)
                and not actions
            )
        decision = SupervisorDecision(
            summary="Approved specialist work is verified complete."
            if complete
            else "Review approved targets and monitor existing specialist work.",
            evidence_event_ids=[UUID(e["id"]) for e in ctx["new_events"]],
            actions=actions[:3],
            objective_satisfied=complete,
            next_review_at=None,
        )
        output = ResponseOutputMessage(
            type="message",
            id="scripted-supervisor",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text", text=decision.model_dump_json(), annotations=[]
                )
            ],
        )
        return ModelResponse(output=[output], usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Supervisor uses bounded Runner.run")
        yield

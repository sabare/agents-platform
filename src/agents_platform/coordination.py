"""Durable case coordination: saved snapshot → SDK proposal → atomic validated commit."""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4, uuid5

from agents import Agent, Model, ModelResponse, ModelSettings, RunConfig, Runner, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import Field, model_validator
from sqlalchemy import select
from temporalio import activity

from agents_platform.client_reviews import review_view
from agents_platform.context import (
    PLATFORM_INSTRUCTIONS,
    ContextBudgetExceeded,
    ExecutionRetrieval,
    ModelBudget,
)
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
    OutboxMessage,
    RecordEvidence,
    Source,
    Task,
    TaskInbox,
    ToolInvocation,
)
from agents_platform.task_contracts import Contract, CreateRecordsTask, RecordRequirementInput
from agents_platform.tasks import TaskError, create_records_task_in_transaction, read_task


class RecordsTarget(Contract):
    patient_contact_id: UUID
    provider_contact_id: UUID
    requirements: list[RecordRequirementInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_assignment(self):
        CreateRecordsTask(case_id=UUID(int=0), objective="Validate target", **self.model_dump())
        return self


class CaseObjective(Contract):
    objective: str = Field(min_length=1, max_length=2000)
    targets: list[RecordsTarget] = Field(default_factory=list, max_length=10)
    expected_revision: int | None = Field(default=None, ge=1)


class SupervisorAction(Contract):
    type: Literal["create_task", "request_human_review"]
    target_index: int | None
    question: str | None


class SupervisorDecision(Contract):
    summary: str = Field(min_length=1, max_length=4000)
    evidence_event_ids: list[UUID]
    actions: list[SupervisorAction] = Field(max_length=3)
    objective_satisfied: bool
    next_review_at: datetime | None


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
        dedupe = f"objective/{case_id}/{key}"
        existing = await session.scalar(
            select(ActivityEvent).where(
                ActivityEvent.tenant_id == tenant_id,
                ActivityEvent.producer == "case-operator",
                ActivityEvent.dedupe_key == dedupe,
            )
        )
        if existing:
            if existing.payload["hash"] != digest(body.model_dump(mode="json")):
                raise TaskError(409, "Idempotency key used for a different objective")
            return coord.group_task_id, False
        if coord and body.expected_revision != coord.revision:
            raise TaskError(409, "Objective revision changed; reload and explicitly replace it")
        if not coord and body.expected_revision is not None:
            raise TaskError(409, "There is no objective to replace")
        if (
            await session.get(
                AgentVersion, ("case_supervisor", coord.agent_version if coord else 2)
            )
            is None
        ):
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
                agent_version=2,
            )
            session.add(coord)
            await session.flush()
        coord.objective_json = body.model_dump(mode="json", exclude={"expected_revision"})
        coord.plan_json = {}
        coord.status, coord.stall_count = "PENDING", 0
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
                "hash": digest(body.model_dump(mode="json")),
                "targets": len(body.targets),
            },
        )
        session.add(event)
        await enqueue_supervisor(session, event, force=True)
        return coord.group_task_id, True


class SupervisorActivities:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings

    @activity.defn
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
        try:
            decision, usage = await self._decide(run, definition)
        except Exception as exc:
            async with self.database.sessions.begin() as session:
                saved = await session.get(Execution, run.id, with_for_update=True)
                if saved and saved.attempt == run.attempt and saved.outcome_json is None:
                    saved.error_type = type(exc).__name__
            raise
        return await self._commit(run, decision, usage)

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
                    {"id": str(s.id), "kind": s.kind, "content": s.content} for s in sources
                ],
                "instructions": memory["instructions"],
                "memory": memory,
                "history": await historical_context(
                    session, tenant, case_id, coord.objective_json.get("objective", "case history")
                ),
                "capabilities": [
                    {
                        "agent_key": "medical_records_collector",
                        "version": 3 if coord.agent_version >= 2 else 2,
                        "stage": "durable_demo",
                    }
                ],
                "agent_definition_hash": definition.definition_hash,
                "inbox_ids": [str(i.id) for i in inbox],
                "deadline": (now() + timedelta(minutes=2)).isoformat(),
            }
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
        if context["plan"].get("decision_count", 0) >= 30:
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
        agent = Agent(
            name=definition["name"],
            model=model,
            instructions=definition["instructions"] + PLATFORM_INSTRUCTIONS,
            tools=retrieval.tools(),
            model_settings=ModelSettings(max_tokens=self.settings.context_output_tokens),
            output_type=SupervisorDecision,
        )
        seconds = max(0.1, (datetime.fromisoformat(context["deadline"]) - now()).total_seconds())
        try:
            async with asyncio.timeout(seconds):
                result = await Runner.run(
                    agent,
                    json.dumps(context, default=str),
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
        usage = result.context_wrapper.usage
        return result.final_output, {
            **{
                k: getattr(usage, k)
                for k in ("requests", "input_tokens", "output_tokens", "total_tokens")
            },
            "context_budget": budget.report(),
        }

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

            def review(question, task_id=None):
                key = digest(
                    {"case": str(coord.case_id), "task": task_id, "kind": "unresolved_review"}
                )
                if not any(r["key"] == key for r in reviews):
                    reviews.append(
                        {
                            "key": key,
                            "task_id": task_id,
                            "question": question,
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
            if truncated or (
                not targets and not coord.objective_json.get("monitor_client_updates")
            ):
                review("Clarify the records targets; a complete task overview is required.")
            if any(
                a.type == "create_task"
                and (a.target_index is None or not 0 <= a.target_index < len(targets))
                for a in decision.actions
            ):
                rejected.append("Assignment refers to an unapproved target")
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
            if not rejected and not truncated:
                for action in decision.actions:
                    if action.type == "request_human_review":
                        review(action.question or "Clarify the next permitted step.")
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
                        agent_version=3 if coord.agent_version >= 2 else 2,
                        objective=f"Collect {record_names} from {provider.display_name}",
                        **target,
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
            if rejected:
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
            if decision.objective_satisfied and not all_covered:
                review("Completion proposed without verified evidence for every approved target.")
            if coord.stall_count >= 2:
                review("Repeated invalid decisions; explicit operator revision is required.")
            uncovered = any(not covering_tasks(t, tasks) for t in targets)
            if not reviews and uncovered and not applied:
                review("No assignment covers the approved scope; clarify the next action.")
            if all_covered and not decision.objective_satisfied and not decision.actions:
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
                "next_review_at": outcome.get("wake_at"),
                "summary": decision.summary,
                "reviews": reviews,
                "actions": applied,
                "rejected": rejected,
                "target_task_ids": [
                    [t["id"] for t in covering_tasks(target, tasks)] for target in targets
                ],
                "last_execution_id": str(run.id),
            }
            coord.plan_json, coord.status = plan, status
            coord.revision += 1
            run.status, run.decision_json, run.outcome_json, run.usage_json, run.finished_at = (
                "SUCCEEDED",
                decision.model_dump(mode="json"),
                outcome,
                usage,
                now(),
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
        if not targets or ctx["tasks_truncated"]:
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
        decision = SupervisorDecision(
            summary="Approved records targets are verified complete."
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

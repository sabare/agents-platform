"""Application-owned intervention handlers. Human input never chooses arbitrary code."""

from datetime import timedelta
from uuid import uuid4

from pydantic import AwareDatetime, model_validator
from sqlalchemy import String, select

from agents_platform.human_requests import ensure_request, fingerprint, now, retire_requests
from agents_platform.models import (
    ActivityEvent,
    Case,
    CaseContact,
    CaseCoordination,
    HumanContinuation,
    HumanDecision,
    RecordsRequest,
    Source,
    Task,
)
from agents_platform.task_contracts import Contract
from agents_platform.tasks import TaskError


class EmptyResponse(Contract):
    pass


class ScheduleFollowUp(Contract):
    follow_up_at: AwareDatetime
    response_due_at: AwareDatetime

    @model_validator(mode="after")
    def bounded(self):
        if not now() < self.follow_up_at <= now() + timedelta(days=90):
            raise ValueError("Choose a future follow-up within 90 days")
        if not self.follow_up_at < self.response_due_at <= self.follow_up_at + timedelta(days=30):
            raise ValueError("Response deadline must be after follow-up and within 30 days")
        return self


def action(name, label, consequence, schema=EmptyResponse):
    return {
        "action": name,
        "label": label,
        "consequence": consequence,
        "payload_schema": schema.model_json_schema(),
    }


CLIENT_ACTIONS = [
    action(
        "ALLOW_COMPLETION",
        "I have handled this report",
        "Acknowledges this report. The same task resumes; other blockers still apply.",
    ),
    action(
        "REQUEST_CHANGES",
        "Give guidance and keep review open",
        "Saves guidance and wakes the agent once. Explicit acknowledgement is still required.",
    ),
]


async def ensure_client_request(session, task, review, report):
    row = await ensure_request(
        session,
        task,
        handler="client_report.v1",
        subject_id=review.id,
        subject_fingerprint=review.report_fingerprint,
        question=review.question,
        context_summary=report.report["message"],
        actions=CLIENT_ACTIONS,
        evidence=[str(report.source_id)],
        dedupe_key=f"client-review/{review.id}",
    )
    return row


async def records_subject(session, task):
    request = await session.scalar(
        select(RecordsRequest).where(
            RecordsRequest.tenant_id == task.tenant_id,
            RecordsRequest.task_id == task.id,
        )
    )
    if not request:
        raise TaskError(409, "Records request is missing")
    evidence = (
        await session.scalars(
            select(Source.id)
            .join(
                ActivityEvent,
                ActivityEvent.payload["source_id"].astext == Source.id.cast(String),
            )
            .where(
                Source.tenant_id == task.tenant_id,
                Source.case_id == task.case_id,
                ActivityEvent.tenant_id == task.tenant_id,
                ActivityEvent.task_id == task.id,
                Source.kind.in_(["SIMULATED_PROVIDER_MESSAGE", "PROVIDER_RESPONSE"]),
            )
            .order_by(Source.id)
        )
    ).all()
    from agents_platform.memory import assemble_memory

    memory = await assemble_memory(
        session,
        task.tenant_id,
        [request.provider_contact_id],
        [task],
        task.case_id,
    )
    from agents_platform.record_receipts import requirement_coverage

    coverage = await requirement_coverage(session, task.tenant_id, request.id)
    value = {
        "coverage": coverage,
        "policy_fingerprint": memory["manifest"]["fingerprint"],
        "request_id": str(request.id),
        "status": request.status,
        "reminder_count": request.reminder_count,
        "max_reminders": request.max_reminders,
        "callback_at": request.callback_at.isoformat() if request.callback_at else None,
        "provider_sources": [str(i) for i in evidence],
    }
    return request, fingerprint(value), value["provider_sources"]


async def ensure_records_request(session, task, reason):
    # Runtime, backfill and manual-contact paths must all reuse the specific
    # source review. A generic form must never offer outreach over an auth hold.
    from agents_platform.communications import ensure_review as communication_review
    from agents_platform.provider_responses import ensure_review as provider_review

    specific = await communication_review(session, task) or await provider_review(session, task)
    if specific:
        return specific
    request, subject, evidence = await records_subject(session, task)
    actions = []
    if (task.agent_version or 0) >= 3 and request.status in ("SENT", "PENDING", "PARTIAL"):
        actions.append(
            action(
                "SCHEDULE_FOLLOW_UP",
                "Authorize one additional follow-up",
                "Schedules one simulated follow-up, then waits until your response deadline. "
                "Contact restrictions are rechecked before sending. Agent definitions stay pinned.",
                ScheduleFollowUp,
            )
        )
    actions.extend(
        [
            action(
                "RECHECK",
                "Recheck after handling the blocker",
                "Reloads evidence and restrictions. Holds and reminder limits remain enforced.",
            ),
            action(
                "STOP_COLLECTION",
                "Stop collection",
                "Cancels collection and pending follow-up. Received evidence is retained.",
            ),
        ]
    )
    return await ensure_request(
        session,
        task,
        handler="records_follow_up.v1",
        subject_id=request.id,
        subject_fingerprint=subject,
        question=reason,
        context_summary=f"Reminders used: {request.reminder_count}/{request.max_reminders}. "
        "Inspect the provider responses and outstanding requirements before deciding.",
        actions=actions,
        evidence=evidence,
        kind="MANUAL_ACTION",
    )


def supervisor_subject(coord):
    questions = [
        {"key": r["key"], "question": r["question"]}
        for r in coord.plan_json.get("reviews", [])
        if not r.get("task_id") and r.get("kind") not in ("EVIDENCE_PENDING", "TECHNICAL")
    ]
    return fingerprint({"objective": coord.objective_json, "questions": questions})


def supervisor_technical_subject(coord):
    return fingerprint(
        {
            "objective": coord.objective_json,
            "reviews": [
                r for r in coord.plan_json.get("reviews", []) if r.get("kind") == "TECHNICAL"
            ],
        }
    )


async def sync_supervisor_request(session, coord):
    from agents_platform.coordination import CaseObjective

    task = await session.get(Task, coord.group_task_id)
    technical = [r for r in coord.plan_json.get("reviews", []) if r.get("kind") == "TECHNICAL"]
    if technical:
        await ensure_request(
            session,
            task,
            handler="supervisor_recovery.v1",
            subject_id=coord.id,
            subject_fingerprint=supervisor_technical_subject(coord),
            question="; ".join(r["question"] for r in technical),
            context_summary="No rejected proposals were applied. Inspect Executions. "
            "Retry keeps the existing objective, tasks, evidence and contact restrictions.",
            actions=[
                action(
                    "RETRY_SUPERVISOR",
                    "Retry supervisor",
                    "Rechecks the unchanged "
                    "approved scope. This does not approve contact or change requirements.",
                )
            ],
            kind="MANUAL_ACTION",
        )
    else:
        await retire_requests(session, task, handler="supervisor_recovery.v1")
    questions = [
        r["question"]
        for r in coord.plan_json.get("reviews", [])
        if not r.get("task_id") and r.get("kind") not in ("EVIDENCE_PENDING", "TECHNICAL")
    ]
    if not questions:
        await retire_requests(session, task, handler="supervisor_objective.v1")
        return
    await ensure_request(
        session,
        task,
        handler="supervisor_objective.v1",
        subject_id=coord.id,
        subject_fingerprint=supervisor_subject(coord),
        question="; ".join(questions),
        context_summary=coord.objective_json.get("objective", "Clarify approved case work."),
        actions=[
            action(
                "REVISE_OBJECTIVE",
                "Update the task details",
                "Checks the details and asks the supervisor to try again.",
                CaseObjective,
            )
        ],
        kind="CLARIFICATION",
    )


async def apply_response(session, task, request, decision):
    if request.handler == "supervisor_recovery.v1":
        if decision.action != "RETRY_SUPERVISOR":
            raise TaskError(422, "Unsupported supervisor recovery action")
        EmptyResponse.model_validate(decision.payload)
        coord = await session.scalar(
            select(CaseCoordination)
            .where(
                CaseCoordination.tenant_id == task.tenant_id,
                CaseCoordination.group_task_id == task.id,
            )
            .with_for_update()
        )
        if not coord or supervisor_technical_subject(coord) != request.subject_fingerprint:
            raise TaskError(409, "Supervisor recovery state changed; refresh before retrying")
        coord.plan_json = {
            **coord.plan_json,
            "citation_replans": 0,
            "automatic_decisions": 0,
            "correction_feedback": None,
            "reviews": [
                r for r in coord.plan_json.get("reviews", []) if r.get("kind") != "TECHNICAL"
            ],
        }
        coord.status = "PENDING"
    elif request.handler == "communication_review.v1":
        from agents_platform.communications import apply_response as apply_communication_response

        await apply_communication_response(session, task, request, decision)
    elif request.handler == "provider_response.v1":
        from agents_platform.provider_responses import apply_response as apply_provider_response

        await apply_provider_response(session, task, request, decision)
    elif request.handler == "records_follow_up.v1":
        await apply_records_response(session, task, request, decision)
    elif request.handler == "supervisor_objective.v1":
        from agents_platform.coordination import CaseObjective, submit_objective_in_transaction

        coord = await session.scalar(
            select(CaseCoordination)
            .where(
                CaseCoordination.tenant_id == task.tenant_id,
                CaseCoordination.group_task_id == task.id,
            )
            .with_for_update()
        )
        if not coord or supervisor_subject(coord) != request.subject_fingerprint:
            raise TaskError(409, "Objective or questions changed; refresh before deciding")
        if decision.action != "REVISE_OBJECTIVE":
            raise TaskError(422, "Unsupported supervisor decision")
        objective = CaseObjective.model_validate(decision.payload)
        objective.expected_revision = coord.revision
        await submit_objective_in_transaction(
            session,
            task.tenant_id,
            task.case_id,
            objective,
            f"human/{decision.id}",
        )
    else:
        raise TaskError(422, "No registered handler for this human request")


async def apply_records_response(session, task, human_request, decision):
    from agents_platform.memory import memory_for_task, stop_contact

    request, subject, _ = await records_subject(session, task)
    if subject != human_request.subject_fingerprint:
        raise TaskError(409, "Provider information changed; refresh and recheck the task")
    if task.status != "BLOCKED":
        raise TaskError(409, "Wait until the records task is blocked, then refresh")
    if decision.action == "SCHEDULE_FOLLOW_UP":
        payload = ScheduleFollowUp.model_validate(decision.payload)
        if (task.agent_version or 0) < 3 or request.status not in ("SENT", "PENDING", "PARTIAL"):
            raise TaskError(409, "An additional follow-up requires an outstanding v3 request")
        if stop_contact(await memory_for_task(session, task)):
            raise TaskError(409, "Resolve the contact restriction before authorizing follow-up")
        pending = await session.scalar(
            select(HumanContinuation.id).where(
                HumanContinuation.tenant_id == task.tenant_id,
                HumanContinuation.task_id == task.id,
                HumanContinuation.status.in_(["PENDING", "APPLIED"]),
            )
        )
        if pending:
            raise TaskError(409, "A human-authorized follow-up is already active")
        decision.payload = payload.model_dump(mode="json")
        session.add(
            HumanContinuation(
                tenant_id=task.tenant_id,
                case_id=task.case_id,
                task_id=task.id,
                decision_id=decision.id,
                ready_at=payload.follow_up_at,
                response_due_at=payload.response_due_at,
                status="PENDING",
            )
        )
        task.status = "WAITING"
        task.progress_json = {
            **task.progress_json,
            "last_reason": "Human-authorized follow-up scheduled; continuation queued.",
        }
    else:
        EmptyResponse.model_validate(decision.payload)
        if decision.action == "STOP_COLLECTION":
            task.status, request.status = "CANCELLED", "CLOSED"
            for row in (
                await session.scalars(
                    select(HumanContinuation).where(
                        HumanContinuation.tenant_id == task.tenant_id,
                        HumanContinuation.task_id == task.id,
                        HumanContinuation.status.in_(["PENDING", "APPLIED"]),
                    )
                )
            ).all():
                row.status = "CANCELLED"
            task.progress_json = {**task.progress_json, "last_reason": decision.reason}
        elif decision.action != "RECHECK":
            raise TaskError(422, "Unsupported records decision")
    # Remove the old question only; the next coordinator checks current task state again.
    coord = await session.scalar(
        select(CaseCoordination)
        .where(
            CaseCoordination.tenant_id == task.tenant_id,
            CaseCoordination.case_id == task.case_id,
        )
        .with_for_update()
    )
    if coord:
        coord.plan_json = {
            **coord.plan_json,
            "reviews": [
                r for r in coord.plan_json.get("reviews", []) if r.get("task_id") != str(task.id)
            ],
        }


async def advance_records_intervention(session, task):
    """Called under policy/task locks. A saved human decision authorizes one effect only."""
    from agents_platform.memory import memory_for_task, stop_contact

    rows = (
        await session.scalars(
            select(HumanContinuation)
            .where(
                HumanContinuation.tenant_id == task.tenant_id,
                HumanContinuation.task_id == task.id,
                HumanContinuation.status.in_(["PENDING", "APPLIED"]),
            )
            .with_for_update()
        )
    ).all()
    if not rows:
        return None
    row = rows[0]
    request, subject, _ = await records_subject(session, task)
    if request.status == "RECEIVED":
        row.status = "FINISHED" if row.status == "APPLIED" else "CANCELLED"
        return None
    decision = await session.get(HumanDecision, row.decision_id)
    case = await session.get(Case, task.case_id)
    participant = await session.scalar(
        select(CaseContact).where(
            CaseContact.tenant_id == task.tenant_id,
            CaseContact.case_id == task.case_id,
            CaseContact.contact_id == request.provider_contact_id,
        )
    )
    from agents_platform.provider_responses import action_blocker

    provider_blocker = await action_blocker(session, task)
    reason = None
    if provider_blocker:
        reason = provider_blocker
    elif not case or case.status != "OPEN":
        reason = "Case closed; human-authorized follow-up cancelled."
    elif not participant or "MEDICAL_PROVIDER" not in participant.roles:
        reason = "Provider role changed; review the intended recipient."
    elif stop_contact(await memory_for_task(session, task)):
        reason = (
            "Contact restriction blocks the human-authorized follow-up. Resolve it and recheck."
        )
    elif row.status == "PENDING" and subject != decision.payload.get(
        "continuation_fingerprint", decision.reviewed_fingerprint
    ):
        reason = "Provider information changed after authorization. Review before scheduling again."
    elif row.status == "PENDING" and now() >= row.response_due_at:
        reason = "Authorized follow-up window expired while unavailable; no late message was sent."
    if not reason and row.status == "PENDING":
        from agents_platform.communications import commitment_due

        promised = await commitment_due(session, task)
        waits = [row.ready_at] + [at for at in (request.callback_at, promised) if at]
        row.ready_at = max(waits)
    if reason:
        row.status = "CANCELLED"
    elif row.status == "PENDING" and row.ready_at <= now():
        from agents_platform.record_receipts import follow_up_content, missing_requirements

        missing = await missing_requirements(session, task, request)
        if not missing:
            row.status = "CANCELLED"
            return None
        source = Source(
            id=uuid4(),
            tenant_id=task.tenant_id,
            case_id=task.case_id,
            source_key=f"human-follow-up/{decision.id}",
            kind="SIMULATED_HUMAN_AUTHORIZED_FOLLOW_UP",
            content={
                "decision_id": str(decision.id),
                "request_id": str(request.id),
                "sent_at": now().isoformat(),
                **follow_up_content(missing),
                "simulated": True,
                "response_due_at": row.response_due_at.isoformat(),
                "authorization_source_id": decision.payload.get("authorization_source_id"),
            },
        )
        session.add(source)
        await session.flush()
        row.source_id, row.status = source.id, "APPLIED"
        session.add(
            ActivityEvent(
                tenant_id=task.tenant_id,
                case_id=task.case_id,
                task_id=task.id,
                event_type="records.human_follow_up_sent",
                producer="human-work",
                dedupe_key=f"follow-up/{decision.id}",
                actor_ref="platform/human-authorized",
                payload={
                    "source_id": str(source.id),
                    "decision_id": str(decision.id),
                    **follow_up_content(missing),
                    "simulated": True,
                },
            )
        )
    if not reason and row.status == "APPLIED" and now() >= row.response_due_at:
        row.status = "FINISHED"
        reason = "The authorized follow-up received no complete records by its response deadline."
    if reason:
        task.status, task.version = "BLOCKED", task.version + 1
        task.progress_json = {**task.progress_json, "last_reason": reason}
        await ensure_records_request(session, task, reason)
        # Ensure the supervisor learns about blocks without a model activation/commit.
        from agents_platform.coordination import enqueue_supervisor

        event = ActivityEvent(
            id=uuid4(),
            tenant_id=task.tenant_id,
            case_id=task.case_id,
            task_id=task.id,
            event_type="human.continuation_blocked",
            producer="human-work",
            dedupe_key=f"blocked/{row.id}",
            actor_ref="platform",
            payload={"reason": reason},
        )
        session.add(event)
        await enqueue_supervisor(session, event)
        return {"status": "BLOCKED", "reason": reason}
    wake = row.ready_at if row.status == "PENDING" else row.response_due_at
    if task.status != "WAITING":
        task.version += 1
    task.status = "WAITING"
    reason = (
        "Waiting for the human-authorized follow-up time."
        if row.status == "PENDING"
        else "Authorized follow-up sent; waiting for provider response."
    )
    task.progress_json = {**task.progress_json, "last_reason": reason}
    return {"status": "WAITING", "wake_at": wake.isoformat(), "reason": reason}


async def backfill_blocked_requests(database):
    """Make pre-upgrade records/objective blockers actionable without running a model."""
    from agents_platform.memory import policy_lock

    async with database.sessions() as session:
        identities = (
            await session.execute(
                select(Task.tenant_id, Task.id).where(
                    Task.status == "BLOCKED",
                    Task.task_type == "medical_records.collect",
                )
            )
        ).all()
        coordinators = (
            await session.execute(
                select(
                    CaseCoordination.tenant_id,
                    CaseCoordination.id,
                ).where(CaseCoordination.status == "NEEDS_REVIEW")
            )
        ).all()
    for tenant_id, task_id in identities:
        async with database.sessions.begin() as session:
            await policy_lock(session, tenant_id)
            task = await session.scalar(
                select(Task)
                .where(
                    Task.tenant_id == tenant_id,
                    Task.id == task_id,
                )
                .with_for_update()
            )
            if task.status == "BLOCKED":
                await ensure_records_request(
                    session,
                    task,
                    task.progress_json.get(
                        "last_reason",
                        "Review this records task and choose the next step.",
                    ),
                )
    for tenant_id, coord_id in coordinators:
        async with database.sessions.begin() as session:
            # Match the coordinator/decision lock order before touching request rows.
            case_id = await session.scalar(
                select(CaseCoordination.case_id).where(
                    CaseCoordination.tenant_id == tenant_id,
                    CaseCoordination.id == coord_id,
                )
            )
            await session.scalar(
                select(Case)
                .where(
                    Case.tenant_id == tenant_id,
                    Case.id == case_id,
                )
                .with_for_update(key_share=True)
            )
            coord = await session.scalar(
                select(CaseCoordination)
                .where(
                    CaseCoordination.tenant_id == tenant_id,
                    CaseCoordination.id == coord_id,
                )
                .with_for_update()
            )
            if coord.status == "NEEDS_REVIEW":
                await sync_supervisor_request(session, coord)

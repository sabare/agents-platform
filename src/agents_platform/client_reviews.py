"""A narrow human decision boundary for sensitive client reports, not consent approval."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select

from agents_platform.models import ActivityEvent, Case, ClientReport, ClientReview, Source, Task
from agents_platform.task_contracts import Contract
from agents_platform.tasks import TaskError


class ReviewDecision(Contract):
    expected_task_version: int = Field(ge=1)
    expected_review_version: int = Field(ge=1)
    decision: Literal["ALLOW_COMPLETION", "REQUEST_CHANGES"]
    reason: str = Field(min_length=1, max_length=2000)


def report_fingerprint(report, source):
    content = {"report": report.report, "changes": report.changes, "source": source.content}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def review_view(review):
    if review is None:
        return None
    return {
        "id": str(review.id),
        "report_id": str(review.report_id),
        "task_id": str(review.task_id),
        "question": review.question,
        "status": review.status,
        "version": review.version,
        "decision": review.decision,
        "reason": review.reason,
        "decided_by": review.decided_by,
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "decision_source_id": str(review.decision_source_id) if review.decision_source_id else None,
    }


async def review_for_report(session, report):
    if not report:
        return None
    return await session.scalar(
        select(ClientReview).where(
            ClientReview.tenant_id == report.tenant_id,
            ClientReview.case_id == report.case_id,
            ClientReview.report_id == report.id,
        )
    )


async def acknowledged(session, report, review):
    if not review or review.status != "RESOLVED":
        return False
    source = await session.scalar(
        select(Source).where(
            Source.tenant_id == report.tenant_id,
            Source.case_id == report.case_id,
            Source.id == report.source_id,
        )
    )
    return source is not None and report_fingerprint(report, source) == review.report_fingerprint


async def create_review(session, task, report, source):
    review = ClientReview(
        id=uuid4(),
        tenant_id=task.tenant_id,
        case_id=task.case_id,
        task_id=task.id,
        report_id=report.id,
        report_fingerprint=report_fingerprint(report, source),
        question="Client self-report needs teammate review: "
        + "; ".join(report.changes["reasons"]),
        status="OPEN",
        version=1,
    )
    session.add(review)
    await session.flush()
    from agents_platform.human_request_handlers import ensure_client_request

    await ensure_client_request(session, task, review, report)
    return review


async def decide_review(session, tenant_id, task_id, review_id, body, key):
    async with session.begin():
        return await decide_review_in_transaction(session, tenant_id, task_id, review_id, body, key)


async def decide_review_in_transaction(
    session, tenant_id, task_id, review_id, body, key, *, shared_response=None
):
    from agents_platform.memory import policy_lock
    from agents_platform.memory_api import wake_task

    await policy_lock(session, tenant_id)
    case_id = await session.scalar(
        select(Task.case_id).where(Task.tenant_id == tenant_id, Task.id == task_id)
    )
    if not case_id:
        raise TaskError(404, "Task not found")
    case = await session.scalar(
        select(Case)
        .where(Case.tenant_id == tenant_id, Case.id == case_id)
        .with_for_update(key_share=True)
    )
    task = await session.scalar(
        select(Task).where(Task.tenant_id == tenant_id, Task.id == task_id).with_for_update()
    )
    review = await session.scalar(
        select(ClientReview)
        .where(
            ClientReview.tenant_id == tenant_id,
            ClientReview.case_id == case_id,
            ClientReview.task_id == task_id,
            ClientReview.id == review_id,
        )
        .with_for_update()
    )
    if not review:
        raise TaskError(404, "Client review not found")
    source_key = f"client-review/{review_id}/{key}"
    existing = await session.scalar(
        select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
    )
    payload = body.model_dump(mode="json")
    if existing:
        if existing.content["request"] != payload:
            raise TaskError(409, "Idempotency-Key used for a different review decision")
        return {
            "review": review_view(review),
            "decision": existing.content,
            "task_version": task.version,
            "replayed": True,
        }
    if case.status != "OPEN" or task.status in ("COMPLETED", "FAILED", "CANCELLED"):
        raise TaskError(409, "Task or case is closed")
    if task.status != "BLOCKED":
        raise TaskError(409, "Wait until this task is blocked for review, then refresh")
    if review.status != "OPEN":
        raise TaskError(409, "This review is already resolved")
    if task.version != body.expected_task_version or review.version != body.expected_review_version:
        raise TaskError(409, "Task or review changed; refresh before deciding")
    report = await session.scalar(
        select(ClientReport).where(
            ClientReport.tenant_id == tenant_id,
            ClientReport.case_id == case_id,
            ClientReport.id == review.report_id,
        )
    )
    source = await session.scalar(
        select(Source).where(
            Source.tenant_id == tenant_id,
            Source.case_id == case_id,
            Source.id == report.source_id,
        )
    )
    if not source or report_fingerprint(report, source) != review.report_fingerprint:
        raise TaskError(409, "Client report changed; this review cannot authorize it")
    from agents_platform.human_request_handlers import ensure_client_request
    from agents_platform.human_requests import fingerprint, now, record_decision

    human_request = await ensure_client_request(session, task, review, report)
    # A decision acknowledges this report only. Contact holds/obligations are untouched.
    review.status = "RESOLVED" if body.decision == "ALLOW_COMPLETION" else "OPEN"
    review.version += 1
    review.decision, review.reason = body.decision, body.reason
    review.decided_by, review.decided_at = "local-operator", datetime.now(timezone.utc)
    decision_source = Source(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        source_key=source_key,
        kind="CLIENT_REVIEW_DECISION",
        content={
            "request": payload,
            "review_id": str(review.id),
            "report_id": str(report.id),
            "report_source_id": str(report.source_id),
            "actor": review.decided_by,
            "decided_at": review.decided_at.isoformat(),
        },
    )
    session.add(decision_source)
    await session.flush()
    review.decision_source_id = decision_source.id
    human_request.status = review.status
    human_request.version += 1
    human_request.resolved_at = now() if review.status == "RESOLVED" else None
    await record_decision(
        session,
        human_request,
        action=body.decision,
        payload={},
        reason=body.reason,
        source_id=decision_source.id,
        key=key,
        body_hash=fingerprint((shared_response or body).model_dump(mode="json")),
    )
    event = ActivityEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        case_id=case_id,
        task_id=task.id,
        event_type="client.review_decided",
        producer="client-review",
        dedupe_key=source_key,
        actor_ref=review.decided_by,
        payload={
            "source_id": str(decision_source.id),
            "review_id": str(review.id),
            "report_id": str(report.id),
            "task_id": str(task.id),
            "decision": body.decision,
            "reason": body.reason,
        },
    )
    session.add(event)
    await session.flush()
    await wake_task(session, task, event)
    return {
        "review": review_view(review),
        "decision": decision_source.content,
        "task_version": task.version,
        "replayed": False,
    }

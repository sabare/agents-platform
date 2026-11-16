"""Thin operator UI API. Comments reuse the persisted event/inbox/outbox primitives."""

import hashlib
from collections import Counter
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import Integer, and_, case, cast, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.capabilities import discover_capabilities
from agents_platform.db import session_dependency
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    CaseCoordination,
    Contact,
    Execution,
    Firm,
    OutboxMessage,
    RecordsRequest,
    Source,
    Task,
    TaskInbox,
)
from agents_platform.task_api import require_operator
from agents_platform.task_contracts import Contract

router = APIRouter(
    prefix="/v1/operator", tags=["dashboard"], dependencies=[Depends(require_operator)]
)
Session = Annotated[AsyncSession, Depends(session_dependency)]


class CommentInput(Contract):
    text: str = Field(min_length=1, max_length=4000)
    mentions: list[str] = Field(default_factory=list, max_length=3)
    wake_agent: bool = False


@router.get("/workspace")
async def workspace(session: Session, request: Request):
    firms = (await session.scalars(select(Firm).order_by(Firm.name))).all()
    return {
        "firms": [{"id": f.id, "name": f.name} for f in firms],
        "mode": request.app.state.settings.agent_mode,
        "temporal": {
            "ui_url": request.app.state.settings.temporal_ui_url,
            "namespace": request.app.state.settings.temporal_namespace,
        },
        "agents": [
            {
                "key": "supervisor",
                "name": "Case supervisor",
                "capability": "Durable case coordinator",
                "description": "Assign specialists and review case progress.",
            },
            *[
                {
                    "key": c["agent_key"],
                    "name": c["name"],
                    "capability": c["description"],
                    "description": c["description"],
                }
                for c in await discover_capabilities().available(session)
            ],
        ],
    }


@router.get("/firms/{tenant_id}/overview")
async def overview(tenant_id: UUID, session: Session):
    if await session.get(Firm, tenant_id) is None:
        raise HTTPException(404, "Firm not found")
    cases = (
        await session.scalars(
            select(Case).where(Case.tenant_id == tenant_id).order_by(Case.display_number)
        )
    ).all()
    counts = (
        await session.execute(
            select(Task.case_id, Task.status, func.count())
            .where(Task.tenant_id == tenant_id, Task.kind == "AGENT")
            .group_by(Task.case_id, Task.status)
        )
    ).all()
    by_case = {c.id: {} for c in cases}
    totals = Counter()
    for case_id, status, count in counts:
        by_case[case_id][status] = count
        totals[status] += count
    usage = (
        await session.execute(
            select(
                func.count(Execution.id),
                func.coalesce(
                    func.sum(cast(Execution.usage_json["total_tokens"].astext, Integer)), 0
                ),
            ).where(Execution.tenant_id == tenant_id)
        )
    ).one()
    contacts = (
        await session.execute(
            select(CaseContact, Contact)
            .join(
                Contact,
                (Contact.id == CaseContact.contact_id)
                & (Contact.tenant_id == CaseContact.tenant_id),
            )
            .where(CaseContact.tenant_id == tenant_id)
        )
    ).all()
    groups = (
        await session.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.kind == "GROUP"))
    ).all()
    versions = (
        await session.scalars(
            select(AgentVersion)
            .where(AgentVersion.agent_key == "medical_records_collector")
            .order_by(AgentVersion.version.desc())
        )
    ).all()
    from agents_platform.supervisor_api import coordination_view

    coordinators = (
        await session.scalars(
            select(CaseCoordination).where(CaseCoordination.tenant_id == tenant_id)
        )
    ).all()
    from agents_platform.models import MonthlyCheckInSchedule
    from agents_platform.monthly_check_ins import schedule_view

    monthly = (
        await session.scalars(
            select(MonthlyCheckInSchedule)
            .where(MonthlyCheckInSchedule.tenant_id == tenant_id)
            .order_by(MonthlyCheckInSchedule.created_at.desc())
            .limit(100)
        )
    ).all()
    return {
        "supervisors": [coordination_view(c) for c in coordinators],
        "monthly_schedules": [schedule_view(r) for r in monthly],
        "counts": dict(totals),
        "parent_count": len(
            {c.group_task_id for c in coordinators}
            | set(
                (
                    await session.scalars(
                        select(Task.parent_task_id).where(
                            Task.tenant_id == tenant_id, Task.parent_task_id.is_not(None)
                        )
                    )
                ).all()
            )
        ),
        "executions": usage[0],
        "tokens": usage[1],
        "cases": [
            {
                "id": c.id,
                "number": c.display_number,
                "title": c.title,
                "status": c.status,
                "counts": by_case[c.id],
            }
            for c in cases
        ],
        "contacts": [
            {"id": c.id, "name": c.display_name, "case_id": cc.case_id, "roles": cc.roles}
            for cc, c in contacts
        ],
        "groups": [
            {
                "id": g.id,
                "case_id": g.case_id,
                "objective": g.objective,
                "progress": g.progress_json,
            }
            for g in groups
        ],
        "versions": [v.version for v in versions if v.version >= 2],
    }


@router.get("/firms/{tenant_id}/tasks")
async def task_list(
    tenant_id: UUID,
    session: Session,
    case_id: UUID | None = None,
    status: str | None = None,
    before: UUID | None = None,
    include_parents: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
):
    parent_ids = (
        select(Task.parent_task_id)
        .where(Task.tenant_id == tenant_id, Task.parent_task_id.is_not(None))
        .correlate(None)
    )
    visible = (
        or_(
            Task.kind == "AGENT",
            and_(
                Task.kind == "GROUP",
                or_(Task.task_type == "case.coordination", Task.id.in_(parent_ids)),
            ),
        )
        if include_parents
        else Task.kind == "AGENT"
    )
    coordination_status = (
        select(CaseCoordination.status)
        .where(CaseCoordination.tenant_id == tenant_id, CaseCoordination.group_task_id == Task.id)
        .scalar_subquery()
    )
    status_map = {
        "MONITORING": "WAITING",
        "NEEDS_REVIEW": "BLOCKED",
        "SATISFIED": "COMPLETED",
        "PENDING": "PENDING",
        "RUNNING": "RUNNING",
        "FAILED": "FAILED",
    }
    effective_status = case(status_map, value=coordination_status, else_=Task.status)
    query = select(Task).where(Task.tenant_id == tenant_id, visible)
    if case_id:
        query = query.where(Task.case_id == case_id)
    if status:
        query = query.where(effective_status == status)
    if before:
        cursor = await session.scalar(
            select(Task).where(Task.id == before, Task.tenant_id == tenant_id)
        )
        if not cursor:
            raise HTTPException(404, "Task cursor not found")
        query = query.where(tuple_(Task.created_at, Task.id) < tuple_(cursor.created_at, cursor.id))
    rows = (
        await session.scalars(
            query.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
        )
    ).all()
    ids = [t.id for t in rows[:limit]]
    coordinators = {
        c.group_task_id: c
        for c in (
            await session.scalars(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == tenant_id, CaseCoordination.group_task_id.in_(ids)
                )
            )
        ).all()
    }
    children = (
        await session.scalars(
            select(Task)
            .where(Task.tenant_id == tenant_id, Task.parent_task_id.in_(ids))
            .order_by(Task.created_at, Task.id)
        )
    ).all()
    subtasks = {}
    for child in children:
        subtasks.setdefault(child.parent_task_id, []).append(
            {
                "id": child.id,
                "objective": child.objective,
                "progress": child.progress_json,
                "status": child.status,
            }
        )
    return {
        "items": [
            {
                "id": t.id,
                "case_id": t.case_id,
                "parent_task_id": t.parent_task_id,
                "objective": t.objective,
                "status": status_map.get(coordinators[t.id].status, t.status)
                if t.id in coordinators
                else t.status,
                "kind": t.kind,
                "task_type": t.task_type,
                "subtasks": subtasks.get(t.id, []),
                "agent_key": "supervisor" if t.id in coordinators else t.agent_key,
                "agent_version": coordinators[t.id].agent_version
                if t.id in coordinators
                else t.agent_version,
                "created_at": t.created_at,
                "progress": t.progress_json,
            }
            for t in rows[:limit]
        ],
        "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.post("/firms/{tenant_id}/tasks/{task_id}/comments", status_code=201)
async def comment(
    tenant_id: UUID,
    task_id: UUID,
    body: CommentInput,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    key = f"comment/{task_id}/{idempotency_key.strip()}"
    async with session.begin():
        task = await session.scalar(
            select(Task)
            .where(Task.tenant_id == tenant_id, Task.id == task_id)
            .with_for_update(key_share=True)
        )
        if not task:
            raise HTTPException(404, "Task not found")
        existing = await session.scalar(
            select(ActivityEvent).where(
                ActivityEvent.tenant_id == tenant_id,
                ActivityEvent.producer == "operator-ui",
                ActivityEvent.dedupe_key == key,
            )
        )
        if existing:
            if existing.payload["hash"] != digest:
                raise HTTPException(409, "Idempotency key already used for a different comment")
            return {
                "id": existing.id,
                "woke_agent": existing.payload["woke_agent"],
                "replayed": True,
            }
        catalog = discover_capabilities()
        if not set(body.mentions) <= {"supervisor", *catalog.capabilities}:
            raise HTTPException(422, "Mention refers to an unregistered agent")
        runnable = False
        if body.wake_agent and task.kind == "AGENT":
            published = await session.get(AgentVersion, (task.agent_key, task.agent_version))
            if published:
                try:
                    catalog.adapters.for_execution(task, published.definition_snapshot)
                    runnable = (
                        published.definition_snapshot.get("capability_stage") == "durable_demo"
                    )
                except ValueError:
                    pass
        if body.wake_agent and (
            task.kind != "AGENT"
            or not runnable
            or task.status in ("COMPLETED", "FAILED", "CANCELLED")
            or task.agent_key not in body.mentions
        ):
            raise HTTPException(
                409, "Wake requires mentioning the assigned runnable agent on an open task"
            )
        event_id = uuid4()
        payload = {
            "text": body.text,
            "mentions": list(dict.fromkeys(body.mentions)),
            "hash": digest,
            "woke_agent": body.wake_agent,
        }
        if body.wake_agent or "supervisor" in body.mentions:
            source = Source(
                id=uuid4(),
                tenant_id=tenant_id,
                case_id=task.case_id,
                source_key=key,
                kind="OPERATOR_COMMENT",
                content={"text": body.text, "author": "local-operator"},
            )
            session.add(source)
            await session.flush()
            payload["source_id"] = str(source.id)
            if body.wake_agent:
                task.version += 1
        session.add(
            ActivityEvent(
                id=event_id,
                tenant_id=tenant_id,
                case_id=task.case_id,
                task_id=task.id,
                event_type="task.comment_added",
                producer="operator-ui",
                dedupe_key=key,
                actor_ref="local-operator",
                payload=payload,
            )
        )
        await session.flush()
        if "supervisor" in body.mentions:
            from agents_platform.coordination import enqueue_supervisor

            woke = await enqueue_supervisor(
                session, await session.get(ActivityEvent, event_id), force=True
            )
            event = await session.get(ActivityEvent, event_id)
            event.payload = {**event.payload, "woke_supervisor": bool(woke)}
        if body.wake_agent:
            session.add(
                TaskInbox(
                    tenant_id=tenant_id, case_id=task.case_id, task_id=task.id, event_id=event_id
                )
            )
            session.add(
                OutboxMessage(
                    tenant_id=tenant_id,
                    event_id=event_id,
                    destination="temporal.wake_task",
                    destination_key=task.workflow_id,
                    payload={"tenant_id": str(tenant_id), "task_id": str(task.id)},
                )
            )
    return {"id": event_id, "woke_agent": body.wake_agent, "replayed": False}


@router.get("/firms/{tenant_id}/activity")
async def activity_feed(
    tenant_id: UUID,
    session: Session,
    case_id: UUID | None = None,
    task_id: UUID | None = None,
    before: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    # Naming is presentation metadata, not another assignment or business update.
    # Keep its audit row, but exclude it before pagination of the operator feed.
    query = select(ActivityEvent).where(
        ActivityEvent.tenant_id == tenant_id,
        ActivityEvent.event_type != "task.title_assigned",
    )
    if task_id:
        selected = await session.scalar(
            select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id)
        )
        if selected is None:
            raise HTTPException(404, "Task not found")
        query = query.where(ActivityEvent.task_id == task_id)
    if case_id:
        query = query.where(ActivityEvent.case_id == case_id)
    if before:
        cursor = await session.scalar(
            select(ActivityEvent).where(
                ActivityEvent.id == before, ActivityEvent.tenant_id == tenant_id
            )
        )
        if not cursor:
            raise HTTPException(404, "Event cursor not found")
        query = query.where(
            tuple_(ActivityEvent.created_at, ActivityEvent.id)
            < tuple_(cursor.created_at, cursor.id)
        )
    events = (
        await session.scalars(
            query.order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(
                limit + 1
            )
        )
    ).all()
    provider_task_ids = [
        e.task_id for e in events[:limit] if e.event_type == "provider.response_received"
    ]
    providers = {}
    if provider_task_ids:
        rows = (
            await session.execute(
                select(RecordsRequest.task_id, Contact.id, Contact.display_name)
                .join(
                    Contact,
                    (Contact.id == RecordsRequest.provider_contact_id)
                    & (Contact.tenant_id == RecordsRequest.tenant_id),
                )
                .where(
                    RecordsRequest.tenant_id == tenant_id,
                    RecordsRequest.task_id.in_(provider_task_ids),
                )
            )
        ).all()
        providers = {
            task_id: {"id": str(contact_id), "name": name} for task_id, contact_id, name in rows
        }
    return {
        "items": [
            {
                "id": e.id,
                "task_id": e.task_id,
                "case_id": e.case_id,
                "type": e.event_type,
                "actor": e.actor_ref,
                "source_actor": (
                    {"id": e.payload["provider_contact_id"], "name": e.payload["provider_name"]}
                    if e.event_type == "provider.response_received"
                    and e.payload.get("provider_name")
                    else providers.get(e.task_id)
                    if e.event_type == "provider.response_received"
                    else None
                ),
                "recorded_by": e.actor_ref
                if e.event_type == "provider.response_received"
                else None,
                "simulated": bool(e.payload.get("simulated") or e.producer == "demo-provider"),
                "payload": e.payload,
                "created_at": e.created_at,
            }
            for e in events[:limit]
        ],
        "next_cursor": events[limit - 1].id if len(events) > limit else None,
    }

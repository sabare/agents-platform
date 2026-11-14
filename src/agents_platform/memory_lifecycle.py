"""Explicit memory lifecycles. Time can expire notes, never contact holds or obligations."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from agents_platform.models import (
    ActivityEvent,
    Case,
    CaseCoordination,
    CaseRestriction,
    Instruction,
    Source,
    Task,
    TaskMemoryItem,
)


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat() if value else None


def normalized(text):
    # Deliberately conservative: semantic similarity cannot authorize merging restrictions.
    return " ".join(text.split()).casefold()


def is_current(item, at=None):
    at = at or now()
    return (
        item.status in ("OPEN", "ACTIVE")
        and (item.effective_from is None or item.effective_from <= at)
        and (getattr(item, "expires_at", None) is None or item.expires_at > at)
    )


def next_transition(item, at=None):
    at = at or now()
    if item.status not in ("OPEN", "ACTIVE"):
        return None
    dates = [item.effective_from, item.review_at, getattr(item, "expires_at", None)]
    return min((d for d in dates if d and d > at), default=None)


def lifecycle_view(item, at=None):
    at = at or now()
    return {
        "recorded_at": iso(item.created_at),
        "effective_from": iso(item.effective_from),
        "closed_at": iso(item.closed_at),
        "review_at": iso(item.review_at),
        "review_due": is_current(item, at) and bool(item.review_at and item.review_at <= at),
        "active": is_current(item, at),
    }


def task_memory_view(item):
    return {
        "id": str(item.id),
        "task_id": str(item.task_id),
        "scope": "TASK",
        "kind": item.kind,
        "text": item.text,
        "status": item.status,
        "revision": item.revision,
        "source_id": str(item.source_id),
        "due_at": iso(item.due_at),
        "expires_at": iso(item.expires_at),
        "supersedes_id": str(item.supersedes_id) if item.supersedes_id else None,
        "superseded_by_id": str(item.superseded_by_id) if item.superseded_by_id else None,
        "resolution_source_id": str(item.resolution_source_id)
        if item.resolution_source_id
        else None,
        "resolution_reason": item.resolution_reason,
        **lifecycle_view(item),
    }


async def notify_scope(session, item, event_type, source_id=None, transition_at=None):
    """Used inside the policy publication lock/transaction, including timer transitions."""
    from agents_platform.memory_api import wake_task

    query = select(Task).where(
        Task.tenant_id == item.tenant_id,
        Task.kind == "AGENT",
        Task.status.not_in(["COMPLETED", "CANCELLED", "FAILED"]),
    )
    if isinstance(item, TaskMemoryItem):
        query = query.where(Task.id == item.task_id)
    elif isinstance(item, CaseRestriction):
        query = query.where(Task.case_id == item.case_id)
    tasks = (await session.scalars(query.order_by(Task.id).with_for_update())).all()
    notified = set()
    payload = {"item_id": str(item.id), "status": item.status, **lifecycle_view(item)}
    if source_id:
        payload["source_id"] = str(source_id)
    for task in tasks:
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=task.tenant_id,
            case_id=task.case_id,
            task_id=task.id,
            event_type=event_type,
            producer="memory-lifecycle",
            actor_ref="platform/memory",
            dedupe_key=(
                f"memory/{item.id}/{item.revision}/{event_type}/{task.id}/{iso(transition_at)}"
            ),
            payload=payload,
        )
        session.add(event)
        await session.flush()
        await wake_task(session, task, event)
        notified.add(task.case_id)
    coords_query = select(CaseCoordination).where(CaseCoordination.tenant_id == item.tenant_id)
    if not isinstance(item, Instruction):
        coords_query = coords_query.where(CaseCoordination.case_id == item.case_id)
    from agents_platform.coordination import enqueue_supervisor

    for coord in (await session.scalars(coords_query)).all():
        if coord.case_id in notified:
            continue
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=coord.tenant_id,
            case_id=coord.case_id,
            task_id=coord.group_task_id,
            event_type=event_type,
            producer="memory-lifecycle",
            actor_ref="platform/memory",
            dedupe_key=(
                f"memory/{item.id}/{item.revision}/{event_type}/{coord.id}/{iso(transition_at)}"
            ),
            payload=payload,
        )
        session.add(event)
        await session.flush()
        await enqueue_supervisor(session, event, force=True)


async def advance_lifecycles(database, limit=100):
    """A bounded restart-safe sweep; action-time readers independently enforce date boundaries."""
    from agents_platform.memory import policy_lock

    processed = 0
    for model in (TaskMemoryItem, CaseRestriction, Instruction):
        if processed >= limit:
            break
        async with database.sessions() as session:
            candidates = (
                await session.execute(
                    select(model.id, model.tenant_id)
                    .where(model.next_transition_at <= now())
                    .order_by(model.next_transition_at, model.id)
                    .limit(limit - processed)
                )
            ).all()
        for item_id, tenant_id in candidates:
            async with database.sessions.begin() as session:
                await policy_lock(session, tenant_id, publishing=True)
                item = await session.get(model, item_id, with_for_update=True)
                at = now()
                if not item or not item.next_transition_at or item.next_transition_at > at:
                    continue
                transition_at = item.next_transition_at
                if isinstance(item, TaskMemoryItem) and item.expires_at and item.expires_at <= at:
                    item.status = "EXPIRED"
                    item.closed_at = item.expires_at
                    item.revision += 1
                item.next_transition_at = next_transition(item, at)
                if isinstance(item, CaseRestriction):
                    case = await session.get(Case, item.case_id, with_for_update=True)
                    case.memory_version += 1
                await notify_scope(
                    session, item, "memory.lifecycle_due", transition_at=transition_at
                )
                processed += 1
    return processed


async def source_lifecycle(session, tenant_id, case_id, source_id):
    """Attach current state to historical memory evidence; history cannot revive old controls."""
    event_item_ids = [
        e
        for e in await session.scalars(
            select(ActivityEvent.payload["item_id"].astext).where(
                ActivityEvent.tenant_id == tenant_id,
                ActivityEvent.case_id == case_id,
                ActivityEvent.payload["source_id"].astext == str(source_id),
                ActivityEvent.payload["item_id"].astext.is_not(None),
                ActivityEvent.event_type.in_(["memory.changed", "case.restriction_changed"]),
            )
        )
    ]
    source = await session.scalar(
        select(Source).where(
            Source.tenant_id == tenant_id, Source.case_id == case_id, Source.id == source_id
        )
    )
    if (
        source
        and source.kind in ("OPERATOR_MEMORY_CHANGE", "OPERATOR_CASE_RESTRICTION")
        and source.content.get("_result", {}).get("item_id")
    ):
        event_item_ids.append(source.content["_result"]["item_id"])
    from uuid import UUID

    from sqlalchemy import or_

    result = []
    for model in (TaskMemoryItem, CaseRestriction):
        matches = [model.source_id == source_id, model.resolution_source_id == source_id]
        if event_item_ids:
            matches.append(model.id.in_([UUID(i) for i in event_item_ids]))
        rows = (
            await session.scalars(
                select(model).where(
                    model.tenant_id == tenant_id, model.case_id == case_id, or_(*matches)
                )
            )
        ).all()
        for item in rows:
            result.append(
                {
                    "item_id": str(item.id),
                    "status": item.status,
                    "superseded_by_id": str(item.superseded_by_id)
                    if item.superseded_by_id
                    else None,
                    "expires_at": iso(getattr(item, "expires_at", None)),
                    **lifecycle_view(item),
                }
            )
    return result

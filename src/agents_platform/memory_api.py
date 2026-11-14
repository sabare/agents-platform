"""Operator-controlled memory writes. Agents cannot publish instructions or lift holds."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import String, cast, or_, select
from sqlalchemy.dialects.postgresql import insert

from agents_platform.context import EvidenceRequest, RetrievalRequest, RetrievalService
from agents_platform.memory import instruction_view, memory_for_task, policy_lock, restriction_view
from agents_platform.memory_history import latest_summary, prepare_query, search_history
from agents_platform.memory_lifecycle import (
    is_current,
    next_transition,
    normalized,
    now,
    task_memory_view,
)
from agents_platform.models import (
    ActivityEvent,
    Case,
    CaseContact,
    CaseCoordination,
    CaseRestriction,
    Instruction,
    InstructionRevision,
    OutboxMessage,
    Source,
    Task,
    TaskInbox,
    TaskMemoryItem,
)
from agents_platform.task_api import Session, require_operator
from agents_platform.task_contracts import Contract

router = APIRouter(
    prefix="/v1/firms/{tenant_id}", tags=["memory"], dependencies=[Depends(require_operator)]
)
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class MemoryChange(Contract):
    expected_version: int = Field(ge=1)
    operation: Literal["ADD", "RESOLVE", "SUPERSEDE", "REVIEW"] = "ADD"
    item_id: UUID | None = None
    kind: Literal["STOP_CONTACT", "OBLIGATION", "NOTE"] | None = None
    text: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)
    due_at: AwareDatetime | None = None
    effective_from: AwareDatetime | None = None
    review_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if not self.text.strip() or (self.reason is not None and not self.reason.strip()):
            raise ValueError("Text and reasons must not be blank")
        if self.operation == "ADD" and (self.kind is None or self.item_id is not None):
            raise ValueError("ADD requires kind and no item_id")
        if self.operation != "ADD" and self.item_id is None:
            raise ValueError("This operation requires item_id")
        if self.operation in ("RESOLVE", "REVIEW"):
            if self.kind or self.due_at or self.effective_from or self.expires_at:
                raise ValueError("Resolution/review cannot replace item fields")
            if self.operation == "RESOLVE" and self.review_at:
                raise ValueError("Resolution cannot schedule a review")
        if self.operation == "SUPERSEDE":
            if not self.kind or not self.reason:
                raise ValueError("SUPERSEDE requires kind and an explicit reason")
            if self.effective_from and self.effective_from > now():
                raise ValueError(
                    "Future replacements require review; the old item must remain active"
                )
        if self.expires_at and self.kind != "NOTE":
            raise ValueError(
                "Only informational notes can expire; holds/obligations require resolution"
            )
        if self.expires_at and self.effective_from and self.expires_at <= self.effective_from:
            raise ValueError("Expiry must follow the effective time")
        if self.review_at and self.effective_from and self.review_at < self.effective_from:
            raise ValueError("Review cannot precede the effective time")
        if self.kind == "STOP_CONTACT" and self.due_at:
            raise ValueError("Contact holds require explicit resolution; they do not expire")
        return self


class PublishInstruction(Contract):
    expected_revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=8000)
    reason: str = Field(min_length=1, max_length=2000)
    status: Literal["ACTIVE", "RETIRED"] | None = None
    effective_from: AwareDatetime | None = None
    review_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if not self.text.strip() or not self.reason.strip():
            raise ValueError("Instruction and reason must not be blank")
        if self.effective_from and self.effective_from > now():
            raise ValueError("Publish effective instructions now; future replacement needs review")
        return self


async def wake_task(session, task, event):
    task.version += 1
    session.add(
        TaskInbox(
            tenant_id=task.tenant_id, case_id=task.case_id, task_id=task.id, event_id=event.id
        )
    )
    session.add(
        OutboxMessage(
            tenant_id=task.tenant_id,
            event_id=event.id,
            destination="temporal.wake_task",
            destination_key=task.workflow_id,
            payload={"tenant_id": str(task.tenant_id), "task_id": str(task.id)},
        )
    )
    from agents_platform.coordination import enqueue_supervisor

    await enqueue_supervisor(session, event, force=True)


@router.get("/tasks/{task_id}/memory")
async def read_memory(
    tenant_id: UUID,
    task_id: UUID,
    session: Session,
    history_offset: int = Query(default=0, ge=0),
    history_limit: int = Query(default=100, ge=1, le=100),
):
    task = await session.scalar(select(Task).where(Task.tenant_id == tenant_id, Task.id == task_id))
    if not task:
        raise HTTPException(404, "Task not found")
    case = await session.get(Case, task.case_id)
    restrictions = (
        await session.scalars(
            select(CaseRestriction)
            .where(CaseRestriction.tenant_id == tenant_id, CaseRestriction.case_id == task.case_id)
            .order_by(CaseRestriction.created_at)
        )
    ).all()
    history = (
        await session.scalars(
            select(TaskMemoryItem)
            .where(TaskMemoryItem.tenant_id == tenant_id, TaskMemoryItem.task_id == task_id)
            .order_by(TaskMemoryItem.created_at.desc(), TaskMemoryItem.id.desc())
            .offset(history_offset)
            .limit(history_limit + 1)
        )
    ).all()
    return {
        "summary": await latest_summary(session, tenant_id, task.case_id),
        "case_memory_version": case.memory_version,
        "case_restrictions": [restriction_view(r) for r in restrictions],
        "task_version": task.version,
        **await memory_for_task(session, task),
        "history": [task_memory_view(i) for i in history[:history_limit]],
        "history_next_offset": history_offset + history_limit
        if len(history) > history_limit
        else None,
    }


@router.post("/tasks/{task_id}/memory")
async def change_memory(
    tenant_id: UUID, task_id: UUID, body: MemoryChange, session: Session, key: Key
):
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    async with session.begin():
        await policy_lock(session, tenant_id, publishing=True)
        task = await session.scalar(
            select(Task).where(Task.tenant_id == tenant_id, Task.id == task_id).with_for_update()
        )
        if not task:
            raise HTTPException(404, "Task not found")
        payload = body.model_dump(mode="json")
        source_key = f"memory/{task_id}/{key}"
        previous = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        if previous:
            if (
                MemoryChange.model_validate(
                    {k: v for k, v in previous.content.items() if k != "_result"}
                ).model_dump(mode="json")
                != payload
            ):
                raise HTTPException(409, "Idempotency-Key was used for a different change")
            if previous.content.get("_result"):
                return {**previous.content["_result"], "replayed": True}
            event = await session.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.tenant_id == tenant_id,
                    ActivityEvent.dedupe_key == source_key,
                    ActivityEvent.producer == "operator",
                )
            )
            return {
                "source_id": str(previous.id),
                "item_id": event.payload["item_id"],
                "deduplicated": event.payload.get("deduplicated", False),
                "replayed": True,
            }
        if body.expires_at and body.expires_at <= now():
            raise HTTPException(422, "New notes must expire in the future")
        if task.kind != "AGENT" or task.status in ("COMPLETED", "CANCELLED", "FAILED"):
            raise HTTPException(409, "Memory changes require an active agent task")
        if task.version != body.expected_version:
            raise HTTPException(409, "Task changed; reload before changing memory")
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            source_key=source_key,
            kind="OPERATOR_MEMORY_CHANGE",
            content=payload,
        )
        session.add(source)
        await session.flush()
        previous_item = None
        if body.item_id:
            previous_item = await session.scalar(
                select(TaskMemoryItem)
                .where(
                    TaskMemoryItem.tenant_id == tenant_id,
                    TaskMemoryItem.task_id == task_id,
                    TaskMemoryItem.id == body.item_id,
                )
                .with_for_update()
            )
            if not previous_item:
                raise HTTPException(404, "Memory item not found on this task")
            if previous_item.status != "OPEN" or (
                previous_item.expires_at and previous_item.expires_at <= now()
            ):
                raise HTTPException(409, "Memory item is no longer open")
        deduplicated = False
        if body.operation in ("RESOLVE", "REVIEW"):
            item = previous_item
            if body.operation == "RESOLVE":
                item.closed_at = now()
                item.status, item.resolution_source_id, item.resolution_reason = (
                    "RESOLVED",
                    source.id,
                    body.text,
                )
            else:
                item.review_at = body.review_at
            item.revision += 1
        else:
            if previous_item and previous_item.kind != body.kind:
                raise HTTPException(422, "Replacement must retain the memory kind")
            rows = (
                await session.scalars(
                    select(TaskMemoryItem).where(
                        TaskMemoryItem.tenant_id == tenant_id,
                        TaskMemoryItem.task_id == task_id,
                        TaskMemoryItem.status == "OPEN",
                    )
                )
            ).all()
            item = next(
                (
                    r
                    for r in rows
                    if r.id != body.item_id
                    and r.kind == body.kind
                    and normalized(r.text) == normalized(body.text)
                    and r.due_at == body.due_at
                    and r.effective_from == body.effective_from
                    and r.expires_at == body.expires_at
                    and r.review_at == body.review_at
                ),
                None,
            )
            deduplicated = item is not None
            if not item:
                if (
                    sum(r.expires_at is None or r.expires_at > now() for r in rows)
                    - bool(previous_item)
                    >= 100
                ):
                    raise HTTPException(409, "Active task memory limit reached; needs review")
                item = TaskMemoryItem(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=task.case_id,
                    task_id=task.id,
                    kind=body.kind,
                    text=body.text,
                    status="OPEN",
                    revision=1,
                    source_id=source.id,
                    due_at=body.due_at,
                    effective_from=body.effective_from,
                    review_at=body.review_at,
                    expires_at=body.expires_at,
                    supersedes_id=previous_item.id if previous_item else None,
                )
                session.add(item)
            if previous_item:
                previous_item.status = "SUPERSEDED"
                previous_item.closed_at = now()
                previous_item.superseded_by_id = item.id
                previous_item.resolution_source_id, previous_item.resolution_reason = (
                    source.id,
                    body.reason,
                )
                previous_item.revision += 1
                previous_item.next_transition_at = None
        item.next_transition_at = next_transition(item)
        await session.flush()
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            task_id=task_id,
            event_type="memory.changed",
            producer="operator",
            actor_ref="local-operator",
            dedupe_key=source_key,
            payload={
                "source_id": str(source.id),
                "item_id": str(item.id),
                "operation": body.operation,
                "deduplicated": deduplicated,
            },
        )
        session.add(event)
        await session.flush()
        await wake_task(session, task, event)
        result = {
            "source_id": str(source.id),
            "item_id": str(item.id),
            "deduplicated": deduplicated,
            "replayed": False,
        }
        source.content = {**payload, "_result": result}
        return result


def revision_values(row, *, approved_by, reason):
    return {
        "id": uuid4(),
        "tenant_id": row.tenant_id,
        "instruction_id": row.id,
        "revision": row.revision,
        "scope": row.scope,
        "provider_contact_id": row.provider_contact_id,
        "text": row.text,
        "approved_by": approved_by,
        "reason": reason,
        "status": row.status,
        "closed_at": row.closed_at,
        "effective_from": row.effective_from,
        "review_at": row.review_at,
    }


@router.get("/instructions/{instruction_id}/revisions")
async def instruction_history(tenant_id: UUID, instruction_id: UUID, session: Session):
    row = await session.scalar(
        select(Instruction).where(
            Instruction.tenant_id == tenant_id, Instruction.id == instruction_id
        )
    )
    if not row:
        raise HTTPException(404, "Instruction not found")
    history = (
        await session.scalars(
            select(InstructionRevision)
            .where(
                InstructionRevision.tenant_id == tenant_id,
                InstructionRevision.instruction_id == instruction_id,
            )
            .order_by(InstructionRevision.revision)
        )
    ).all()
    return {
        "current": instruction_view(row),
        "history": [
            {
                **instruction_view(i),
                "id": str(i.instruction_id),
                "approved_by": i.approved_by,
                "reason": i.reason,
                "published_at": i.created_at.isoformat(),
                "active": i.revision == row.revision and is_current(row),
                "historical_revision": i.revision != row.revision,
                "superseded_by_revision": i.revision + 1 if i.revision < row.revision else None,
            }
            for i in history
        ],
    }


@router.post("/instructions/{instruction_id}/revisions")
async def publish_instruction(
    tenant_id: UUID, instruction_id: UUID, body: PublishInstruction, session: Session
):
    async with session.begin():
        await policy_lock(session, tenant_id, publishing=True)
        row = await session.scalar(
            select(Instruction)
            .where(Instruction.tenant_id == tenant_id, Instruction.id == instruction_id)
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "Instruction not found")
        if row.revision != body.expected_revision:
            raise HTTPException(409, "Instruction changed; reload its current revision")
        # Seeded legacy instructions are preserved when first revised, too.
        await session.execute(
            insert(InstructionRevision)
            .values(
                **revision_values(
                    row, approved_by="legacy-import", reason="Previous current instruction"
                )
            )
            .on_conflict_do_nothing()
        )
        row.text, row.revision = body.text, row.revision + 1
        previous_status = row.status
        row.status = body.status or row.status
        if row.status == "RETIRED" and previous_status != "RETIRED":
            row.closed_at = now()
        elif row.status == "ACTIVE":
            row.closed_at = None
        if "effective_from" in body.model_fields_set:
            row.effective_from = body.effective_from
        if "review_at" in body.model_fields_set:
            row.review_at = body.review_at
        row.next_transition_at = next_transition(row)
        await session.flush()
        session.add(
            InstructionRevision(
                **revision_values(row, approved_by="local-operator", reason=body.reason)
            )
        )
        query = select(Task).where(
            Task.tenant_id == tenant_id,
            Task.kind == "AGENT",
            Task.status.not_in(["COMPLETED", "CANCELLED", "FAILED"]),
        )
        tasks = (await session.scalars(query.order_by(Task.id).with_for_update(of=Task))).all()
        if row.provider_contact_id:
            from agents_platform.domains import default_registry

            registry = default_registry()
            applicable = []
            for task in tasks:
                domain = await registry.for_task(task).load(session, task)
                if row.provider_contact_id in domain.instruction_provider_ids:
                    applicable.append(task)
            tasks = applicable
        for task in tasks:
            event = ActivityEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                case_id=task.case_id,
                task_id=task.id,
                event_type="instruction.published",
                producer="operator",
                actor_ref="local-operator",
                dedupe_key=f"instruction/{row.id}/{row.revision}/{task.id}",
                payload={"instruction_id": str(row.id), "revision": row.revision},
            )
            session.add(event)
            await session.flush()
            await wake_task(session, task, event)
        # Objectives with no delegated tasks must also see the new policy.
        coords = (
            await session.scalars(
                select(CaseCoordination)
                .where(CaseCoordination.tenant_id == tenant_id)
                .order_by(CaseCoordination.case_id)
            )
        ).all()
        affected_cases = {t.case_id for t in tasks}
        for coord in coords:
            if coord.case_id in affected_cases:
                continue
            if row.provider_contact_id and str(row.provider_contact_id) not in {
                t["provider_contact_id"] for t in coord.objective_json.get("targets", [])
            }:
                continue
            event = ActivityEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                case_id=coord.case_id,
                task_id=coord.group_task_id,
                event_type="instruction.published",
                producer="operator",
                actor_ref="local-operator",
                dedupe_key=f"instruction/{row.id}/{row.revision}/{coord.group_task_id}",
                payload={"instruction_id": str(row.id), "revision": row.revision},
            )
            session.add(event)
            await session.flush()
            from agents_platform.coordination import enqueue_supervisor

            await enqueue_supervisor(session, event, force=True)
        return instruction_view(row)


class CaseRestrictionChange(Contract):
    expected_version: int = Field(ge=0)
    operation: Literal["ADD", "RESOLVE", "SUPERSEDE", "REVIEW"] = "ADD"
    item_id: UUID | None = None
    provider_contact_id: UUID | None = None
    text: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)
    effective_from: AwareDatetime | None = None
    review_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if not self.text.strip() or (self.reason is not None and not self.reason.strip()):
            raise ValueError("Text and reasons must not be blank")
        if self.operation == "ADD" and self.item_id:
            raise ValueError("ADD must not supply item_id")
        if self.operation != "ADD" and not self.item_id:
            raise ValueError("This operation requires item_id")
        if self.operation in ("RESOLVE", "REVIEW") and (
            self.provider_contact_id or self.effective_from
        ):
            raise ValueError("Resolution/review cannot change scope or effective date")
        if self.operation == "RESOLVE" and self.review_at:
            raise ValueError("Resolution cannot schedule review")
        if self.operation == "SUPERSEDE":
            if not self.reason:
                raise ValueError("SUPERSEDE requires an explicit reason")
            if self.effective_from and self.effective_from > now():
                raise ValueError(
                    "Future replacements require review; the old hold must remain active"
                )
        if self.review_at and self.effective_from and self.review_at < self.effective_from:
            raise ValueError("Review cannot precede the effective time")
        return self


@router.post("/cases/{case_id}/restrictions")
async def change_case_restriction(
    tenant_id: UUID, case_id: UUID, body: CaseRestrictionChange, session: Session, key: Key
):
    if not key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    async with session.begin():
        # Exclusive publication lock also protects future tasks and action-time checks.
        await policy_lock(session, tenant_id, publishing=True)
        case = await session.scalar(
            select(Case)
            .where(Case.tenant_id == tenant_id, Case.id == case_id)
            .with_for_update(key_share=True)
        )
        if not case:
            raise HTTPException(404, "Case not found")
        source_key, payload = f"case-restriction/{case_id}/{key}", body.model_dump(mode="json")
        previous = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        if previous:
            previous_payload = CaseRestrictionChange.model_validate(
                {k: v for k, v in previous.content.items() if k != "_result"}
            ).model_dump(mode="json")
            if previous_payload != payload:
                raise HTTPException(409, "Idempotency-Key was used for a different change")
            return {
                **previous.content.get("_result", {"source_id": str(previous.id)}),
                "replayed": True,
            }
        if case.memory_version != body.expected_version:
            raise HTTPException(409, "Case memory changed; reload before changing restrictions")
        if body.provider_contact_id:
            participant = await session.scalar(
                select(CaseContact).where(
                    CaseContact.tenant_id == tenant_id,
                    CaseContact.case_id == case_id,
                    CaseContact.contact_id == body.provider_contact_id,
                )
            )
            if not participant or "MEDICAL_PROVIDER" not in participant.roles:
                raise HTTPException(422, "Provider must belong to this case")
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=case_id,
            source_key=source_key,
            kind="OPERATOR_CASE_RESTRICTION",
            content=payload,
        )
        session.add(source)
        await session.flush()
        previous_item = None
        if body.item_id:
            previous_item = await session.scalar(
                select(CaseRestriction)
                .where(
                    CaseRestriction.tenant_id == tenant_id,
                    CaseRestriction.case_id == case_id,
                    CaseRestriction.id == body.item_id,
                )
                .with_for_update()
            )
            if not previous_item:
                raise HTTPException(404, "Case restriction not found")
            if previous_item.status != "OPEN":
                raise HTTPException(409, "Case restriction is no longer open")
        deduplicated = False
        if body.operation in ("RESOLVE", "REVIEW"):
            restriction = previous_item
            if body.operation == "RESOLVE":
                restriction.status = "RESOLVED"
                restriction.closed_at = now()
                restriction.resolution_source_id, restriction.resolution_reason = (
                    source.id,
                    body.text,
                )
            else:
                restriction.review_at = body.review_at
            restriction.revision += 1
        else:
            provider_id = (
                previous_item.provider_contact_id if previous_item else body.provider_contact_id
            )
            if (
                previous_item
                and body.provider_contact_id
                and body.provider_contact_id != provider_id
            ):
                raise HTTPException(422, "Replacement must retain the restriction scope")
            rows = (
                await session.scalars(
                    select(CaseRestriction).where(
                        CaseRestriction.tenant_id == tenant_id,
                        CaseRestriction.case_id == case_id,
                        CaseRestriction.status == "OPEN",
                        CaseRestriction.provider_contact_id == provider_id,
                    )
                )
            ).all()
            restriction = next(
                (
                    r
                    for r in rows
                    if r.id != body.item_id
                    and normalized(r.text) == normalized(body.text)
                    and r.effective_from == body.effective_from
                    and r.review_at == body.review_at
                ),
                None,
            )
            deduplicated = restriction is not None
            if not restriction:
                restriction = CaseRestriction(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=case_id,
                    provider_contact_id=provider_id,
                    text=body.text,
                    source_id=source.id,
                    revision=1,
                    status="OPEN",
                    effective_from=body.effective_from,
                    review_at=body.review_at,
                    supersedes_id=previous_item.id if previous_item else None,
                )
                session.add(restriction)
            if previous_item:
                previous_item.closed_at = now()
                previous_item.status, previous_item.superseded_by_id = "SUPERSEDED", restriction.id
                previous_item.resolution_source_id, previous_item.resolution_reason = (
                    source.id,
                    body.reason,
                )
                previous_item.revision += 1
                previous_item.next_transition_at = None
        restriction.next_transition_at = next_transition(restriction)
        await session.flush()
        case.memory_version += 1
        tasks = (
            await session.scalars(
                select(Task)
                .where(
                    Task.tenant_id == tenant_id,
                    Task.case_id == case_id,
                    Task.kind == "AGENT",
                    Task.status.not_in(["COMPLETED", "FAILED", "CANCELLED"]),
                )
                .order_by(Task.id)
                .with_for_update()
            )
        ).all()
        for task in tasks:
            event = ActivityEvent(
                id=uuid4(),
                tenant_id=tenant_id,
                case_id=case_id,
                task_id=task.id,
                event_type="case.restriction_changed",
                producer="operator",
                actor_ref="local-operator",
                dedupe_key=f"{source_key}/{task.id}",
                payload={
                    "source_id": str(source.id),
                    "item_id": str(restriction.id),
                    "operation": body.operation,
                    "deduplicated": deduplicated,
                },
            )
            session.add(event)
            await session.flush()
            await wake_task(session, task, event)
        if not tasks:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == tenant_id, CaseCoordination.case_id == case_id
                )
            )
            if coord:
                event = ActivityEvent(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=case_id,
                    task_id=coord.group_task_id,
                    event_type="case.restriction_changed",
                    producer="operator",
                    actor_ref="local-operator",
                    dedupe_key=source_key,
                    payload={"source_id": str(source.id)},
                )
                session.add(event)
                await session.flush()
                from agents_platform.coordination import enqueue_supervisor

                await enqueue_supervisor(session, event, force=True)
        result = {
            "item_id": str(restriction.id),
            "source_id": str(source.id),
            "case_memory_version": case.memory_version,
            "deduplicated": deduplicated,
            "replayed": False,
        }
        source.content = {**payload, "_result": result}
        return result


@router.get("/cases/{case_id}/memory")
async def case_memory(tenant_id: UUID, case_id: UUID, session: Session):
    case = await session.scalar(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    if not case:
        raise HTTPException(404, "Case not found")
    items = (
        await session.scalars(
            select(CaseRestriction)
            .where(CaseRestriction.tenant_id == tenant_id, CaseRestriction.case_id == case_id)
            .order_by(CaseRestriction.created_at)
        )
    ).all()
    return {
        "case_memory_version": case.memory_version,
        "restrictions": [restriction_view(r) for r in items],
        "summary": await latest_summary(session, tenant_id, case_id),
    }


@router.get("/cases/{case_id}/memory/reviews")
async def due_memory_reviews(
    tenant_id: UUID,
    case_id: UUID,
    session: Session,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
):
    case = await session.scalar(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    if not case:
        raise HTTPException(404, "Case not found")
    # This is an operator queue, not a model-context cap. Pagination never omits active rules.
    entries = []
    for model, view in ((TaskMemoryItem, task_memory_view), (CaseRestriction, restriction_view)):
        rows = (
            await session.scalars(
                select(model).where(
                    model.tenant_id == tenant_id,
                    model.case_id == case_id,
                    model.status == "OPEN",
                    model.review_at <= now(),
                )
            )
        ).all()
        entries.extend(view(r) for r in rows if is_current(r))
    provider_ids = (
        await session.scalars(
            select(CaseContact.contact_id).where(
                CaseContact.tenant_id == tenant_id,
                CaseContact.case_id == case_id,
                CaseContact.roles.contains(["MEDICAL_PROVIDER"]),
            )
        )
    ).all()
    from agents_platform.memory import instructions_for

    entries.extend(
        instruction_view(r)
        for r in await instructions_for(session, tenant_id, provider_ids)
        if r.review_at and r.review_at <= now()
    )
    entries.sort(key=lambda i: (i["review_at"], i["id"]))
    return {
        "items": entries[offset : offset + limit],
        "next_offset": offset + limit if offset + limit < len(entries) else None,
        "total": len(entries),
        "notice": "Review dates do not lift restrictions.",
    }


@router.get("/cases/{case_id}/memory/search")
async def search_case_memory(
    tenant_id: UUID,
    case_id: UUID,
    request: Request,
    session: Session,
    q: str = Query(min_length=1, max_length=500),
):
    case = await session.scalar(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    if not case:
        raise HTTPException(404, "Case not found")
    q = q.strip()
    if not q:
        raise HTTPException(422, "Search query must not be blank")
    exact = await session.scalar(
        select(Source.id)
        .where(
            Source.tenant_id == tenant_id,
            Source.case_id == case_id,
            or_(cast(Source.id, String) == q, Source.source_key == q),
        )
        .limit(1)
    )
    if exact:
        return await search_history(session, tenant_id, case_id, q)
    # Finish authorization transaction before calling the embedding provider.
    await session.rollback()
    await prepare_query(
        request.app.state.database, request.app.state.settings, tenant_id, case_id, q
    )
    return await search_history(session, tenant_id, case_id, q)


@router.get("/cases/{case_id}/sources/{source_id}")
async def read_memory_source(tenant_id: UUID, case_id: UUID, source_id: UUID, session: Session):
    source = await session.scalar(
        select(Source).where(
            Source.tenant_id == tenant_id, Source.case_id == case_id, Source.id == source_id
        )
    )
    if not source:
        raise HTTPException(404, "Source not found")
    return {
        "id": str(source.id),
        "kind": source.kind,
        "content": source.content,
        "recorded_at": source.created_at.isoformat(),
    }


@router.post("/cases/{case_id}/context/retrieve")
async def retrieve_case_context(
    tenant_id: UUID,
    case_id: UUID,
    body: RetrievalRequest,
    request: Request,
):
    service = RetrievalService(
        request.app.state.database, request.app.state.settings, tenant_id, case_id
    )
    try:
        return await service.retrieve(body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/cases/{case_id}/context/read")
async def expand_case_evidence(
    tenant_id: UUID,
    case_id: UUID,
    body: EvidenceRequest,
    request: Request,
):
    service = RetrievalService(
        request.app.state.database, request.app.state.settings, tenant_id, case_id
    )
    try:
        result = await service.read(body)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if "error" in result:
        raise HTTPException(409 if "changed" in result["error"] else 404, result["error"])
    return result

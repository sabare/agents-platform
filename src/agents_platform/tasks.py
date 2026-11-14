"""Task creation commits assignment, business request, event, and dispatch together."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    Contact,
    Firm,
    OutboxMessage,
    RecordEvidence,
    RecordRequirement,
    RecordsRequest,
    Source,
    Task,
    TaskInbox,
)
from agents_platform.task_contracts import (
    CreateRecordsTask,
    ProviderUpdate,
    RecordsRequestView,
    RequirementView,
    TaskView,
)


class TaskError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def read_task(session: AsyncSession, tenant_id: UUID, task_id: UUID) -> TaskView:
    task = await session.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
    if task is None:
        raise TaskError(404, "Task not found")
    request = await session.scalar(
        select(RecordsRequest).where(
            RecordsRequest.tenant_id == tenant_id, RecordsRequest.task_id == task_id
        )
    )
    request_view = None
    if request is not None:
        requirements = (
            await session.scalars(
                select(RecordRequirement)
                .where(
                    RecordRequirement.tenant_id == tenant_id,
                    RecordRequirement.request_id == request.id,
                )
                .order_by(RecordRequirement.record_type, RecordRequirement.id)
            )
        ).all()
        request_view = RecordsRequestView(
            id=request.id,
            patient_contact_id=request.patient_contact_id,
            provider_contact_id=request.provider_contact_id,
            status=request.status,
            reminders_enabled=(task.agent_version or 0) >= 3,
            reminder_count=request.reminder_count,
            max_reminders=request.max_reminders,
            next_reminder_at=request.next_reminder_at,
            requirements=[
                RequirementView(
                    id=item.id,
                    record_type=item.record_type,
                    all_dates=item.all_dates,
                    from_date=item.from_date,
                    through_date=item.through_date,
                )
                for item in requirements
            ],
        )
    fields = {
        name: getattr(task, name)
        for name in TaskView.model_fields
        if name
        not in (
            "records_request",
            "client_check_in",
            "check_in_series",
            "monthly_check_in_schedule",
        )
    }
    from agents_platform.client_check_in import check_in_view

    check_in = await check_in_view(session, task) if task.task_type == "client.check_in" else None
    series = None
    if task.task_type == "client.check_in_series":
        from agents_platform.check_in_series import series_view
        from agents_platform.models import CheckInSeries

        row = await session.scalar(
            select(CheckInSeries).where(
                CheckInSeries.tenant_id == tenant_id, CheckInSeries.group_task_id == task.id
            )
        )
        series = series_view(row) if row else None
        if row:
            from agents_platform.models import ClientCheckIn

            occurrences = await session.execute(
                select(ClientCheckIn, Task)
                .join(
                    Task,
                    (Task.id == ClientCheckIn.task_id)
                    & (Task.tenant_id == ClientCheckIn.tenant_id),
                )
                .where(ClientCheckIn.tenant_id == tenant_id, ClientCheckIn.series_id == row.id)
                .order_by(ClientCheckIn.occurrence)
                .limit(100)
            )
            series["occurrences"] = [
                {
                    "task_id": str(t.id),
                    "occurrence": c.occurrence,
                    "status": t.status,
                    "scheduled_at": c.scheduled_at.isoformat(),
                    "sent_at": c.sent_at.isoformat() if c.sent_at else None,
                }
                for c, t in occurrences
            ]
    monthly = None
    if task.task_type == "client.monthly_schedule":
        from agents_platform.models import MonthlyCheckInSchedule
        from agents_platform.monthly_check_ins import read_schedule

        row = await session.scalar(
            select(MonthlyCheckInSchedule).where(
                MonthlyCheckInSchedule.tenant_id == tenant_id,
                MonthlyCheckInSchedule.group_task_id == task.id,
            )
        )
        monthly = await read_schedule(session, row) if row else None
    return TaskView(
        **fields,
        records_request=request_view,
        client_check_in=check_in,
        check_in_series=series,
        monthly_check_in_schedule=monthly,
    )


async def create_records_task(
    session: AsyncSession,
    tenant_id: UUID,
    body: CreateRecordsTask,
    creation_key: str,
) -> tuple[TaskView, bool]:
    async with session.begin():
        return await create_records_task_in_transaction(session, tenant_id, body, creation_key)


async def create_records_task_in_transaction(
    session: AsyncSession,
    tenant_id: UUID,
    body: CreateRecordsTask,
    creation_key: str,
    actor_ref: str = "local-operator",
) -> tuple[TaskView, bool]:
    digest = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    async def replay(existing: Task) -> tuple[TaskView, bool]:
        if existing.creation_hash != digest:
            raise TaskError(409, "Idempotency key already used for a different assignment")
        return await read_task(session, tenant_id, existing.id), False

    existing = await session.scalar(
        select(Task).where(Task.tenant_id == tenant_id, Task.creation_key == creation_key)
    )
    if existing is not None:
        return await replay(existing)

    if await session.get(Firm, tenant_id) is None:
        raise TaskError(404, "Firm not found")
    case = await session.scalar(
        select(Case).where(Case.tenant_id == tenant_id, Case.id == body.case_id).with_for_update()
    )
    if case is None:
        raise TaskError(404, "Case not found")
    if case.status != "OPEN":
        raise TaskError(409, "New tasks require an open case")
    if body.parent_task_id is not None:
        parent = await session.scalar(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.case_id == body.case_id,
                Task.id == body.parent_task_id,
            )
        )
        if parent is None:
            raise TaskError(404, "Parent task not found in this case")
        if parent.kind != "GROUP" or parent.status in ("COMPLETED", "CANCELLED", "FAILED"):
            raise TaskError(409, "Parent must be an open grouping task")
    agent = await session.get(AgentVersion, ("medical_records_collector", body.agent_version))
    if agent is None:
        raise TaskError(422, "Agent version is not published")

    for contact_id, required_roles in (
        (body.patient_contact_id, {"CLIENT", "PATIENT"}),
        (body.provider_contact_id, {"MEDICAL_PROVIDER"}),
    ):
        participant = await session.scalar(
            select(CaseContact).where(
                CaseContact.tenant_id == tenant_id,
                CaseContact.case_id == body.case_id,
                CaseContact.contact_id == contact_id,
            )
        )
        if participant is None:
            raise TaskError(404, "Contact not found in this case")
        if not required_roles.intersection(participant.roles):
            raise TaskError(422, "Contact does not have the required case role")
    patient = await session.get(Contact, body.patient_contact_id)
    if patient.kind != "PERSON":
        raise TaskError(422, "Patient must be a person")

    task_id, request_id, event_id = uuid4(), uuid4(), uuid4()
    requirement_ids = [uuid4() for _ in body.requirements]
    workflow_id = f"firm/{tenant_id}/task/{task_id}"
    inserted = await session.scalar(
        insert(Task)
        .values(
            id=task_id,
            tenant_id=tenant_id,
            case_id=body.case_id,
            parent_task_id=body.parent_task_id,
            kind="AGENT",
            task_type="medical_records.collect",
            objective=body.objective,
            agent_key=agent.agent_key,
            agent_version=agent.version,
            input_schema="records_assignment.v1",
            input_json={"request_id": str(request_id)},
            progress_json={
                "schema": "records_progress.v1",
                "outstanding_requirement_ids": [str(value) for value in requirement_ids],
                "open_commitments": [],
            },
            completion_json={
                "schema": "records_completion.v1",
                "require_reviewed_evidence": True,
            },
            status="PENDING",
            version=1,
            workflow_id=workflow_id,
            creation_key=creation_key,
            creation_hash=digest,
        )
        .on_conflict_do_nothing(index_elements=[Task.tenant_id, Task.creation_key])
        .returning(Task.id)
    )
    # A concurrent request can win after the first lookup; its whole transaction
    # is committed before this statement resolves. Never duplicate its children.
    if inserted is None:
        existing = await session.scalar(
            select(Task).where(Task.tenant_id == tenant_id, Task.creation_key == creation_key)
        )
        return await replay(existing)

    await session.execute(
        insert(RecordsRequest).values(
            id=request_id,
            tenant_id=tenant_id,
            case_id=body.case_id,
            task_id=task_id,
            patient_contact_id=body.patient_contact_id,
            provider_contact_id=body.provider_contact_id,
            status="DRAFT",
        )
    )
    await session.execute(
        insert(RecordRequirement),
        [
            dict(
                id=requirement_id,
                tenant_id=tenant_id,
                case_id=body.case_id,
                request_id=request_id,
                **item.model_dump(),
            )
            for requirement_id, item in zip(requirement_ids, body.requirements, strict=True)
        ],
    )
    await session.execute(
        insert(ActivityEvent).values(
            id=event_id,
            tenant_id=tenant_id,
            case_id=body.case_id,
            task_id=task_id,
            event_type="task.created",
            producer="task-service",
            dedupe_key=f"task-created/{task_id}",
            actor_ref=actor_ref,
            payload={"schema": "task_created.v1", "assignment": body.model_dump(mode="json")},
        )
    )
    await session.execute(
        insert(OutboxMessage).values(
            tenant_id=tenant_id,
            event_id=event_id,
            destination="temporal.start_task",
            destination_key=workflow_id,
            payload={
                "schema": "start_task.v1",
                "tenant_id": str(tenant_id),
                "task_id": str(task_id),
            },
        )
    )
    view = await read_task(session, tenant_id, task_id)
    if not creation_key.startswith("supervisor/"):
        from agents_platform.coordination import enqueue_supervisor

        event = await session.get(ActivityEvent, event_id)
        await enqueue_supervisor(session, event)
    return view, True


async def receive_provider_update(session, tenant_id, task_id, body: ProviderUpdate, key: str):
    """Demo-only intake: store a provider message and synthetic records, then wake the task."""
    async with session.begin():
        task = await session.scalar(
            select(Task).where(Task.tenant_id == tenant_id, Task.id == task_id).with_for_update()
        )
        if task is None:
            raise TaskError(404, "Task not found")
        source_key = f"provider/{task_id}/{key}"
        existing = await session.scalar(
            select(Source).where(Source.tenant_id == tenant_id, Source.source_key == source_key)
        )
        content = {**body.model_dump(mode="json"), "simulated": True}
        if existing:
            if existing.content != content:
                raise TaskError(409, "Idempotency key used for a different provider update")
            return existing.id, False
        if task.status in ("COMPLETED", "CANCELLED", "FAILED"):
            raise TaskError(409, "Task is closed; late responses require review")
        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == tenant_id, RecordsRequest.task_id == task_id
            )
        )
        if request is None:
            raise TaskError(422, "Task is not a medical-records task")
        if request.status == "RECEIVED" and body.status != "RECEIVED":
            raise TaskError(409, "This demo does not automatically retract received records")
        source = Source(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            source_key=source_key,
            kind="SIMULATED_PROVIDER_MESSAGE",
            content=content,
        )
        session.add(source)
        await session.flush()
        if body.status == "RECEIVED":
            requirements = (
                await session.scalars(
                    select(RecordRequirement).where(
                        RecordRequirement.tenant_id == tenant_id,
                        RecordRequirement.request_id == request.id,
                    )
                )
            ).all()
            for requirement in requirements:
                artifact = Source(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    case_id=task.case_id,
                    source_key=f"{source_key}/record/{requirement.id}",
                    kind="SIMULATED_RECORD",
                    content={
                        "requirement_id": str(requirement.id),
                        "record_type": requirement.record_type,
                        "from_date": str(requirement.from_date),
                        "through_date": str(requirement.through_date),
                        "all_dates": requirement.all_dates,
                        "provider_message_id": str(source.id),
                        "simulated": True,
                    },
                )
                session.add(artifact)
                await session.flush()
                session.add(
                    RecordEvidence(
                        tenant_id=tenant_id,
                        case_id=task.case_id,
                        requirement_id=requirement.id,
                        source_id=artifact.id,
                    )
                )
        request.status = body.status
        if body.status == "RECEIVED":
            request.next_reminder_at = None
        task.version += 1  # An older in-flight execution must re-read this update.
        event = ActivityEvent(
            id=uuid4(),
            tenant_id=tenant_id,
            case_id=task.case_id,
            task_id=task_id,
            event_type="provider.response_received",
            producer="demo-provider",
            dedupe_key=source_key,
            actor_ref="local-operator",
            payload={"source_id": str(source.id), "status": body.status},
        )
        session.add(event)
        await session.flush()
        session.add(
            TaskInbox(tenant_id=tenant_id, case_id=task.case_id, task_id=task_id, event_id=event.id)
        )
        session.add(
            OutboxMessage(
                tenant_id=tenant_id,
                event_id=event.id,
                destination="temporal.wake_task",
                destination_key=task.workflow_id,
                payload={"tenant_id": str(tenant_id), "task_id": str(task_id)},
            )
        )
        return source.id, True

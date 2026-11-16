"""Existing domains register through exactly the same contract as new agents."""

from uuid import UUID

from pydantic import AwareDatetime, Field

from agents_platform.capabilities import AgentCapability
from agents_platform.client_check_in import ClientCheckInAdapter
from agents_platform.medical_records import MedicalRecordsAdapter
from agents_platform.models import Task
from agents_platform.registry import load_definition
from agents_platform.task_contracts import Contract, RecordRequirementInput


class RecordsAssignment(Contract):
    patient_contact_id: UUID
    provider_contact_id: UUID
    requirements: list[RecordRequirementInput] = Field(min_length=1, max_length=20)


class CheckInAssignment(Contract):
    client_contact_id: UUID
    scheduled_at: AwareDatetime
    message: str = Field(min_length=1, max_length=2000)


async def create_records(session, scope, assignment, key):
    from sqlalchemy import select

    from agents_platform.coordination import covering_tasks
    from agents_platform.task_contracts import CreateRecordsTask
    from agents_platform.tasks import TaskError, create_records_task_in_transaction, read_task

    rows = (
        await session.scalars(
            select(Task)
            .where(
                Task.tenant_id == scope.tenant_id,
                Task.case_id == scope.case_id,
                Task.task_type == "medical_records.collect",
            )
            .order_by(Task.created_at, Task.id)
        )
    ).all()
    views = [
        (await read_task(session, scope.tenant_id, row.id)).model_dump(mode="json") for row in rows
    ]
    target = assignment.model_dump(mode="json")
    matches = covering_tasks(target, views)
    if matches:
        match = matches[0]
        if (
            match["status"] in ("BLOCKED", "FAILED")
            or match["parent_task_id"] != str(scope.parent_task_id)
            or match["agent_version"] != scope.agent_version
        ):
            raise TaskError(409, "Existing records work needs review; do not create a replacement")
        return await session.get(Task, UUID(match["id"]))
    if any(
        view["records_request"]
        and all(
            view["records_request"][key] == target[key]
            for key in ("patient_contact_id", "provider_contact_id")
        )
        for view in views
    ):
        raise TaskError(409, "Overlapping or cancelled records work requires operator review")
    view, _ = await create_records_task_in_transaction(
        session,
        scope.tenant_id,
        CreateRecordsTask(
            case_id=scope.case_id,
            parent_task_id=scope.parent_task_id,
            objective=scope.objective,
            agent_version=scope.agent_version,
            **assignment.model_dump(),
        ),
        key,
        actor_ref=scope.actor_ref,
    )
    return await session.get(Task, view.id)


async def create_check_in(session, scope, assignment, key):
    from agents_platform.check_in_api import CreateCheckIn, create_check_in_in_transaction

    view = await create_check_in_in_transaction(
        session,
        scope.tenant_id,
        CreateCheckIn(
            case_id=scope.case_id,
            parent_task_id=scope.parent_task_id,
            objective=scope.objective,
            **assignment.model_dump(),
        ),
        key,
    )
    return await session.get(Task, view.id)


CAPABILITIES = (
    AgentCapability(
        definition=load_definition("medical_records_collector", 5),
        adapter=MedicalRecordsAdapter(),
        assignment_type=RecordsAssignment,
        description=(
            "Collect medical records, surgical notes, bills or imaging from a case provider. "
            "Requires explicit patient, provider and document/date scope; do not invent it."
        ),
        create_task=create_records,
    ),
    AgentCapability(
        definition=load_definition("client_check_in", 2),
        adapter=ClientCheckInAdapter(),
        assignment_type=CheckInAssignment,
        description=(
            "Send one scheduled check-in to a case client, wait for an attributed reply, "
            "and report meaningful updates or request human review. Not a recurring schedule."
        ),
        create_task=create_check_in,
    ),
)

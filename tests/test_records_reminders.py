"""Real simulated reminder actions with occurrence keys and bounded backend policy."""

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import func, select
from test_memory import activation, change, runtime
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.execution import StaleExecution
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    CaseCoordination,
    RecordsRequest,
    ToolInvocation,
)
from agents_platform.registry import definition_hash, load_definition

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def create_v3(client, fixture, key="reminders"):
    result = post(client, fixture, key=key, body={**fixture["assignment"], "agent_version": 3})
    assert result.status_code == 201, result.text
    return result.json()


async def due(database, task):
    async with database.sessions.begin() as session:
        request = await session.scalar(
            select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
        )
        request.next_reminder_at = datetime.now(timezone.utc) - timedelta(seconds=1)


async def invoke(tool, arguments):
    raw = json.dumps(arguments)
    return json.loads(
        await tool.on_invoke_tool(
            ToolContext(context=None, tool_name=tool.name, tool_call_id="test", tool_arguments=raw),
            raw,
        )
    )


async def test_three_reminders_then_block_but_late_receipt_still_completes(setup, database_url):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "WAITING"
        for cycle in range(1, 4):
            await due(database, task)
            result = await worker.execute_task(activation(fixture, task, cycle))
            assert result["status"] == ("BLOCKED" if cycle == 3 else "WAITING")
        assert "reminder limit" in result["reason"]
        async with database.sessions() as session:
            reminders = (
                await session.scalars(
                    select(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_records_reminder",
                    )
                    .order_by(ToolInvocation.created_at)
                )
            ).all()
            assert [r.logical_key for r in reminders] == [
                f"records-reminder/{i}" for i in range(1, 4)
            ]
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(
                        ActivityEvent.task_id == UUID(task["id"]),
                        ActivityEvent.event_type == "records.reminder_sent",
                    )
                )
                == 3
            )
        update = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "received"},
            json={"status": "RECEIVED", "message": "Synthetic records ready."},
        )
        assert update.status_code == 201, update.text
        assert (await worker.execute_task(activation(fixture, task, 4)))["status"] == "COMPLETED"
    finally:
        await database.close()


async def test_reminder_action_validates_due_occurrence_and_reuses_saved_effect(
    setup, database_url
):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        run = await worker._prepare(activation(fixture, task))
        tools = {t.name: t for t in worker._tools(run)}
        with pytest.raises(ValueError):
            await invoke(tools["send_records_reminder"], {"occurrence": 1})
        await invoke(tools["send_records_request"], {})
        rejected = await invoke(tools["send_records_reminder"], {"occurrence": 1})
        assert rejected["status"] == "NOT_DUE" and not rejected["action_taken"]
        await due(database, task)
        with pytest.raises(ValueError):
            await invoke(tools["send_records_reminder"], {"occurrence": 2})
        first = await invoke(tools["send_records_reminder"], {"occurrence": 1})
        replay = await invoke(tools["send_records_reminder"], {"occurrence": 1})
        assert replay == first and replay["simulated"]
        rejected = await invoke(tools["send_records_reminder"], {"occurrence": 2})
        assert rejected["status"] == "NOT_DUE" and not rejected["action_taken"]
        with pytest.raises(ValueError):
            await invoke(tools["send_records_reminder"], {"occurrence": 4})
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.reminder_count == 1
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_records_reminder",
                    )
                )
                == 1
            )
    finally:
        await database.close()


async def test_contact_hold_fences_inflight_reminder_and_old_agent_versions_stay_pinned(
    setup, database_url
):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    legacy = post(client, fixture, key="legacy").json()
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        await due(database, task)
        run = await worker._prepare(activation(fixture, task, 1))
        current = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}", headers=AUTH
        ).json()
        hold = change(client, fixture, current)
        assert hold.status_code == 200, hold.text
        tool = next(t for t in worker._tools(run) if t.name == "send_records_reminder")
        with pytest.raises(StaleExecution):
            await invoke(tool, {"occurrence": 1})
        await worker._commit(run, activation(fixture, task, 1), None, {})
        assert (await worker.execute_task(activation(fixture, task, 2)))["status"] == "BLOCKED"
        old = await worker._prepare(activation(fixture, legacy))
        assert old.definition["version"] == 2
        assert "send_records_reminder" not in {t.name for t in worker._tools(old)}
        async with database.sessions() as session:
            published = await session.get(AgentVersion, ("medical_records_collector", 2))
            assert published.definition_hash == definition_hash(
                load_definition("medical_records_collector", 2)
            )
            assert not await session.scalar(
                select(ToolInvocation.id).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_reminder",
                )
            )
    finally:
        await database.close()


async def test_new_supervisor_delegates_v3_without_upgrading_existing_v1(setup, database_url):
    from test_supervisor import activation as case_activation
    from test_supervisor import submit

    from agents_platform.coordination import SupervisorActivities
    from agents_platform.models import Task

    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    supervisor = SupervisorActivities(database, worker.settings)
    try:
        assert submit(client, fixture).status_code == 201
        await supervisor.coordinate_case(case_activation(fixture))
        async with database.sessions() as session:
            assigned = await session.scalar(
                select(Task).where(Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT")
            )
            assert assigned.agent_version == 5
        assert submit(client, foreign).status_code == 201
        async with database.sessions.begin() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(foreign["firm_id"])
                )
            )
            coord.agent_version = 1  # Simulate a previously published/pinned coordinator.
        await supervisor.coordinate_case(case_activation(foreign))
        async with database.sessions() as session:
            assigned = await session.scalar(
                select(Task).where(Task.tenant_id == UUID(foreign["firm_id"]), Task.kind == "AGENT")
            )
            assert assigned.agent_version == 2
    finally:
        await database.close()


async def test_due_reminder_cannot_be_silently_ignored_forever(setup, database_url):
    from agents_platform.task_contracts import NextAction, RecordsDecision

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        await due(database, task)
        for cycle in range(1, 4):
            prepared = await worker._prepare(activation(fixture, task, cycle))
            decision = RecordsDecision(
                summary="Wait",
                observations=[],
                received_source_ids=[],
                next_action=NextAction(type="wait", follow_up_seconds=30, reason="Waiting"),
            )
            outcome = await worker._commit(prepared, activation(fixture, task, cycle), decision, {})
            assert outcome["status"] == ("CONTINUE" if cycle < 3 else "BLOCKED")
        assert "reminder" in outcome["reason"]
    finally:
        await database.close()


async def test_temporal_restart_and_retry_do_not_repeat_a_reminder(
    setup, database_url, monkeypatch
):
    from uuid import uuid4

    from temporalio.client import Client
    from test_task_persistence import task_worker, wait_for_status

    from agents_platform.config import Settings
    from agents_platform.dispatch import dispatch_once
    from agents_platform.execution import TaskActivities

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, _ = runtime(database_url)
    settings = Settings(
        _env_file=None, agent_mode="scripted", demo_follow_up_seconds=2, demo_max_activations=20
    )
    temporal = await Client.connect("localhost:7233")
    queue = f"reminder-{uuid4()}"
    original = TaskActivities._commit
    failed = False

    async def fail_once(self, run, event, decision, usage):
        nonlocal failed
        if event["cycle"] >= 1 and not failed:
            failed = True
            raise RuntimeError("Simulated crash after persisted reminder effect")
        return await original(self, run, event, decision, usage)

    monkeypatch.setattr(TaskActivities, "_commit", fail_once)
    try:
        async with task_worker(database, settings, temporal, queue):
            await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
            assert await wait_for_status(database, task["id"], {"WAITING", "FAILED"}) == "WAITING"
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.max_reminders = 1
            request.next_reminder_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        async with task_worker(database, settings, temporal, queue):
            assert await wait_for_status(database, task["id"], {"BLOCKED", "FAILED"}) == "BLOCKED"
        assert failed
        async with database.sessions() as session:
            reminders = (
                await session.scalars(
                    select(ToolInvocation).where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_records_reminder",
                    )
                )
            ).all()
            assert len(reminders) == 1 and reminders[0].logical_key == "records-reminder/1"
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.reminder_count == 1
    finally:
        await temporal.get_workflow_handle(task["workflow_id"]).terminate()
        await database.close()


@pytest.mark.parametrize("change_kind", ["closed_case", "provider_role", "patient_role"])
async def test_records_contact_rechecks_case_and_participant_roles_after_preparation(
    setup, database_url, change_kind
):
    from agents_platform.domains import ActionStateChanged
    from agents_platform.models import Case, CaseContact

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        prepared = await worker._prepare(activation(fixture, task))
        async with database.sessions.begin() as session:
            if change_kind == "closed_case":
                case = await session.get(Case, UUID(task["case_id"]))
                case.status = "CLOSED"
            else:
                contact_id = fixture["assignment"][
                    "provider_contact_id"
                    if change_kind == "provider_role"
                    else "patient_contact_id"
                ]
                participant = await session.scalar(
                    select(CaseContact).where(
                        CaseContact.tenant_id == UUID(fixture["firm_id"]),
                        CaseContact.case_id == UUID(task["case_id"]),
                        CaseContact.contact_id == UUID(contact_id),
                    )
                )
                participant.roles = ["OTHER"]
        tool = next(t for t in worker._tools(prepared) if t.name == "send_records_request")
        with pytest.raises(ActionStateChanged):
            await invoke(tool, {})
        assert (await worker._commit(prepared, activation(fixture, task), None, {}))[
            "status"
        ] == "CONTINUE"
        assert (await worker.execute_task(activation(fixture, task, 1)))["status"] == "BLOCKED"
        if change_kind == "closed_case":
            from test_human_requests import decide, requests

            review = requests(client, fixture, task)[0]
            stopped = decide(client, fixture, review, action="STOP_COLLECTION")
            assert stopped.status_code == 200, stopped.text
        async with database.sessions() as session:
            assert not await session.scalar(
                select(ToolInvocation.id).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_request",
                )
            )
    finally:
        await database.close()

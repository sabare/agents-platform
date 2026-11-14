"""Callback intake, contact fencing and durable Temporal delivery in disposable databases."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_memory import activation, change, runtime
from test_records_reminders import create_v3, invoke
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.models import (
    ActivityEvent,
    Execution,
    OutboxMessage,
    RecordsRequest,
    Source,
    Task,
    ToolInvocation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def response(client, fixture, task, key="callback", **body):
    return client.post(
        f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "status": "PENDING",
            "message": "Call back Friday at 2pm",
            "callback_timezone": "America/New_York",
            **body,
        },
    )


async def test_promise_saved_atomically_no_model_or_contact_before_due_and_idempotent(
    setup, database_url
):
    client, fixture, foreign = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        before = datetime.now(timezone.utc)
        due = before + timedelta(days=4)
        body = {"callback_at": due.isoformat()}
        saved = response(client, fixture, task, **body)
        assert saved.status_code == 201, saved.text
        assert response(client, fixture, task, **body).status_code == 200
        assert (
            response(
                client, fixture, task, callback_at=(due + timedelta(days=1)).isoformat()
            ).status_code
            == 409
        )
        assert response(client, foreign, task, key="foreign", **body).status_code == 404
        outcome = await worker.execute_task(activation(fixture, task, 1))
        assert outcome["status"] == "WAITING"
        assert datetime.fromisoformat(outcome["wake_at"]) == due
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at == request.next_reminder_at == due
            assert request.reminder_count == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Execution)
                    .where(Execution.task_id == UUID(task["id"]))
                )
                == 1
            )
            event = await session.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.task_id == UUID(task["id"]),
                    ActivityEvent.event_type == "provider.response_received",
                )
            )
            assert datetime.fromisoformat(event.payload["callback_at"]) == due
            assert await session.scalar(
                select(OutboxMessage.id).where(
                    OutboxMessage.event_id == event.id,
                    OutboxMessage.destination == "temporal.wake_task",
                )
            )
            source = await session.get(Source, UUID(event.payload["source_id"]))
            assert source.content["callback_schedule"]["timezone"] == "America/New_York"
    finally:
        await database.close()


async def test_due_callback_contacts_once_and_resets_normal_reminders(setup, database_url):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        due = datetime.now(timezone.utc) + timedelta(hours=2)
        assert response(client, fixture, task, callback_at=due.isoformat()).status_code == 201
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.callback_at = request.next_reminder_at = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
        run = await worker._prepare(activation(fixture, task, 2))
        tool = next(t for t in worker._tools(run) if t.name == "send_records_reminder")
        first = await invoke(tool, {"occurrence": 1})
        assert first["callback_at"] and first["callback_timezone"] == "America/New_York"
        assert await invoke(tool, {"occurrence": 1}) == first
        # The real agent turn reuses that persisted effect after a retry.
        outcome = await worker.execute_task(activation(fixture, task, 2))
        assert outcome["status"] == "WAITING"
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at is None and request.callback_timezone is None
            assert request.reminder_count == 1 and request.next_reminder_at > datetime.now(
                timezone.utc
            )
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


async def test_reschedule_fences_inflight_actions_and_plain_pending_keeps_appointment(
    setup, database_url
):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.next_reminder_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        run = await worker._prepare(activation(fixture, task, 1))
        due = datetime.now(timezone.utc) + timedelta(days=2)
        assert response(client, fixture, task, callback_at=due.isoformat()).status_code == 201
        from agents_platform.execution import StaleExecution

        tool = next(t for t in worker._tools(run) if t.name == "send_records_reminder")
        with pytest.raises(StaleExecution):
            await invoke(tool, {"occurrence": 1})
        assert (
            response(client, fixture, task, key="pending", message="Still processing").status_code
            == 201
        )
        assert (
            datetime.fromisoformat(
                (await worker.execute_task(activation(fixture, task, 2)))["wake_at"]
            )
            == due
        )
        later = due + timedelta(days=2)
        assert (
            response(client, fixture, task, key="later", callback_at=later.isoformat()).status_code
            == 201
        )
        assert (
            datetime.fromisoformat(
                (await worker.execute_task(activation(fixture, task, 3)))["wake_at"]
            )
            == later
        )
    finally:
        await database.close()


async def test_records_arrive_early_cancel_appointment_and_complete(setup, database_url):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        assert (
            response(
                client,
                fixture,
                task,
                callback_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            ).status_code
            == 201
        )
        assert (
            response(
                client,
                fixture,
                task,
                key="received",
                status="RECEIVED",
                message="All records ready",
            ).status_code
            == 201
        )
        assert (await worker.execute_task(activation(fixture, task, 2)))["status"] == "COMPLETED"
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at is None and request.next_reminder_at is None
            assert request.reminder_count == 0
    finally:
        await database.close()


async def test_callback_never_bypasses_contact_hold(setup, database_url):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        assert (
            response(
                client,
                fixture,
                task,
                callback_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            ).status_code
            == 201
        )
        assert change(client, fixture, task).status_code == 200
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.callback_at = request.next_reminder_at = datetime.now(timezone.utc) - timedelta(
                seconds=1
            )
        assert (await worker.execute_task(activation(fixture, task, 1)))["status"] == "BLOCKED"
        async with database.sessions() as session:
            assert not await session.scalar(
                select(ToolInvocation.id).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_reminder",
                )
            )
    finally:
        await database.close()


async def test_clear_callback_restores_normal_followups_without_sending_early(setup, database_url):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        assert (
            response(
                client,
                fixture,
                task,
                callback_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            ).status_code
            == 201
        )
        assert (
            response(
                client,
                fixture,
                task,
                key="clear",
                message="No appointment needed",
                clear_callback=True,
            ).status_code
            == 201
        )
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at is None and request.callback_timezone is None
            assert request.next_reminder_at > datetime.now(timezone.utc)
    finally:
        await database.close()


def test_invalid_schedules_do_not_write_provider_sources_or_events(setup, database_url):
    from test_task_persistence import connect

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    for fields in [
        {"message": "Call back next week"},
        {"callback_at": "2000-01-01T09:00:00Z"},
        {"callback_at": "2027-01-01T09:00:00"},
        {"callback_timezone": "bad/zone"},
    ]:
        result = response(client, fixture, task, **fields)
        assert result.status_code == 422, result.text
    with connect(database_url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM sources WHERE tenant_id = %s "
                "AND kind = 'SIMULATED_PROVIDER_MESSAGE'",
                (UUID(fixture["firm_id"]),),
            ).fetchone()[0]
            == 0
        )
    legacy = post(client, fixture, key="v2").json()
    assert (
        response(
            client,
            fixture,
            legacy,
            callback_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        ).status_code
        == 409
    )


@pytest.mark.parametrize("early_receipt", [True, False])
async def test_temporal_callback_survives_restart_then_receipt_or_timer_runs_it(
    setup, database_url, early_receipt
):
    from temporalio.client import Client
    from test_task_persistence import task_worker, wait_for_status

    from agents_platform.config import Settings
    from agents_platform.dispatch import dispatch_once

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, _ = runtime(database_url)
    temporal = await Client.connect("localhost:7233")
    settings = Settings(
        _env_file=None, agent_mode="scripted", demo_follow_up_seconds=1, demo_max_activations=30
    )
    queue = f"callback-{uuid4()}"
    try:
        # Create the callback before dispatching the task, keeping a long absolute timer.
        due = datetime.now(timezone.utc) + (
            timedelta(days=3) if early_receipt else timedelta(seconds=8)
        )
        assert response(client, fixture, task, callback_at=due.isoformat()).status_code == 201
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.max_reminders = 1
        async with task_worker(database, settings, temporal, queue):
            while await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"])):
                pass
            await asyncio.sleep(2)
        async with task_worker(database, settings, temporal, queue):
            await asyncio.sleep(2)
            async with database.sessions() as session:
                assert not await session.scalar(
                    select(ToolInvocation.id).where(ToolInvocation.task_id == UUID(task["id"]))
                )
                assert (await session.get(Task, UUID(task["id"]))).status == "WAITING"
            if early_receipt:
                assert (
                    response(
                        client,
                        fixture,
                        task,
                        key="received",
                        status="RECEIVED",
                        message="All records arrived",
                    ).status_code
                    == 201
                )
                while await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"])):
                    pass
                assert (
                    await wait_for_status(database, task["id"], {"COMPLETED", "FAILED"})
                    == "COMPLETED"
                )
                assert (
                    await asyncio.wait_for(
                        temporal.get_workflow_handle(task["workflow_id"]).result(), 15
                    )
                )["status"] == "COMPLETED"
            else:
                assert (
                    await wait_for_status(database, task["id"], {"BLOCKED", "FAILED"}) == "BLOCKED"
                )
                async with database.sessions() as session:
                    sent = (
                        await session.scalars(
                            select(ToolInvocation).where(
                                ToolInvocation.task_id == UUID(task["id"]),
                                ToolInvocation.tool_name == "send_records_reminder",
                            )
                        )
                    ).all()
                    assert len(sent) == 1 and sent[0].created_at >= due
                    assert datetime.fromisoformat(sent[0].result_json["callback_at"]) == due
    finally:
        try:
            await temporal.get_workflow_handle(task["workflow_id"]).terminate()
        except Exception:
            pass
        await database.close()


async def test_callback_rolls_back_with_failed_outbox_and_limit_is_not_bypassed(
    setup, database_url, monkeypatch
):
    from sqlalchemy.ext.asyncio import AsyncSession

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        original = AsyncSession.add

        def fail_delivery(self, instance, *args, **kwargs):
            if isinstance(instance, OutboxMessage) and instance.destination == "temporal.wake_task":
                raise RuntimeError("outbox unavailable")
            return original(self, instance, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "add", fail_delivery)
            result = response(client, fixture, task)
            assert result.status_code == 500
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at is None and request.status == "SENT"
            assert not await session.scalar(
                select(Source.id).where(
                    Source.tenant_id == UUID(fixture["firm_id"]),
                    Source.kind == "SIMULATED_PROVIDER_MESSAGE",
                )
            )
            request.reminder_count = request.max_reminders
        assert response(client, fixture, task).status_code == 409
    finally:
        await database.close()


async def test_natural_callback_is_resolved_once_and_replay_does_not_move_it(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        saved = response(client, fixture, task)
        assert saved.status_code == 201, saved.text
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            due = request.callback_at
            from zoneinfo import ZoneInfo

            local = due.astimezone(ZoneInfo("America/New_York"))
            assert local.weekday() == 4 and local.hour == 14

        class AfterCallback(datetime):
            @classmethod
            def now(cls, tz=None):
                return (due + timedelta(days=1)).astimezone(tz)

        monkeypatch.setattr("agents_platform.tasks.datetime", AfterCallback)
        replay = response(client, fixture, task)
        assert replay.status_code == 200 and replay.json()["source_id"] == saved.json()["source_id"]
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at == due
    finally:
        await database.close()

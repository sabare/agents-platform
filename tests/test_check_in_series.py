"""Bounded recurring work, no overlapping contact, and pause/stop fencing."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import func, select
from test_check_in import activation, create, path, reply, runtime
from test_client_reviews import decide
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.check_in_series import SeriesActivities
from agents_platform.execution import StaleExecution
from agents_platform.models import CheckInSeries, ClientCheckIn, OutboxMessage, Task, ToolInvocation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def root(fixture):
    return (
        f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/check-in-series"
    )


def create_series(client, fixture, key="series", **changes):
    return client.post(
        root(fixture),
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "client_contact_id": fixture["assignment"]["patient_contact_id"],
            "interval_seconds": 60,
            "max_occurrences": 2,
            "first_due_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            **changes,
        },
    )


def series_activation(fixture, row):
    return {
        "tenant_id": fixture["firm_id"],
        "case_id": fixture["assignment"]["case_id"],
        "series_id": row["id"],
    }


def control(client, fixture, row, action, key="control"):
    return client.post(
        root(fixture) + f"/{row['id']}/control",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "action": action,
            "expected_version": row["version"],
            "reason": f"Operator requested {action.lower()}.",
        },
    )


async def current(database, row):
    async with database.sessions() as session:
        from agents_platform.check_in_series import series_view

        return series_view(await session.get(CheckInSeries, UUID(row["id"])))


async def due(database, row):
    async with database.sessions.begin() as session:
        saved = await session.get(CheckInSeries, UUID(row["id"]))
        saved.next_due_at = datetime.now(timezone.utc) - timedelta(seconds=1)


async def test_series_end_bound_scope_and_replay(setup, database_url):
    client, fixture, foreign = setup
    url = root(fixture)
    body = {
        "client_contact_id": fixture["assignment"]["patient_contact_id"],
        "interval_seconds": 60,
        "first_due_at": datetime.now(timezone.utc).isoformat(),
        "max_occurrences": 2,
    }
    assert client.post(url, json=body).status_code == 401
    for changes in (
        {"max_occurrences": None},
        {"interval_seconds": 0},
        {"max_occurrences": 101},
        {"first_due_at": "2026-01-01T00:00:00"},
    ):
        assert (
            client.post(
                url, headers={**AUTH, "Idempotency-Key": "invalid"}, json={**body, **changes}
            ).status_code
            == 422
        )
    response = client.post(url, headers={**AUTH, "Idempotency-Key": "same"}, json=body)
    assert response.status_code == 201, response.text
    row = response.json()
    replay = client.post(url, headers={**AUTH, "Idempotency-Key": "same"}, json=body)
    assert replay.json()["id"] == row["id"] and replay.json()["replayed"]
    assert (
        client.post(
            url, headers={**AUTH, "Idempotency-Key": "same"}, json={**body, "max_occurrences": 3}
        ).status_code
        == 409
    )
    assert create_series(client, fixture, key="parallel").status_code == 409
    assert create(client, fixture, key="manual-overlap").status_code == 409
    assert (
        client.post(
            root(foreign) + f"/{row['id']}/control",
            headers={**AUTH, "Idempotency-Key": "foreign"},
            json={"action": "STOP", "expected_version": 1, "reason": "Stop"},
        ).status_code
        == 404
    )
    assert (
        create_series(
            client, foreign, client_contact_id=fixture["assignment"]["patient_contact_id"]
        ).status_code
        == 422
    )


async def test_two_child_tasks_wait_for_review_then_finish_without_catchup(setup, database_url):
    client, fixture, _ = setup
    row = create_series(client, fixture).json()
    database, worker = runtime(database_url)
    scheduler = SeriesActivities(database)
    request = series_activation(fixture, row)
    try:
        await scheduler.advance_series(request)
        first_state = await current(database, row)
        assert first_state["occurrence_count"] == 1 and first_state["next_due_at"] is None
        first = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{first_state['active_task_id']}", headers=AUTH
        ).json()
        assert first["parent_task_id"] == row["group_task_id"]
        assert (await worker.execute_task(activation(first)))["status"] == "WAITING"
        assert reply(client, first, pain_score=9, requests_help=True).status_code == 201
        assert (await worker.execute_task(activation(first, 1)))["status"] == "BLOCKED"
        for _ in range(3):
            await scheduler.advance_series(request)
        assert (await current(database, row))["occurrence_count"] == 1
        first = client.get(path(first), headers=AUTH).json()
        assert decide(client, first).status_code == 200
        assert (await worker.execute_task(activation(first, 2)))["status"] == "COMPLETED"
        result = await scheduler.advance_series(request)
        updated = await current(database, row)
        assert updated["active_task_id"] is None and updated["occurrence_count"] == 1
        assert datetime.fromisoformat(result["wake_at"]) > datetime.now(timezone.utc)
        # Advancing time in persisted scheduling state must create just one occurrence.
        await due(database, row)
        await scheduler.advance_series(request)
        await scheduler.advance_series(request)
        updated = await current(database, row)
        assert updated["occurrence_count"] == 2 and updated["active_task_id"] != first["id"]
        second = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{updated['active_task_id']}", headers=AUTH
        ).json()
        assert (await worker.execute_task(activation(second)))["status"] == "WAITING"
        assert reply(client, second, pain_score=3).status_code == 201
        assert (await worker.execute_task(activation(second, 1)))["status"] == "COMPLETED"
        assert (await scheduler.advance_series(request))["status"] == "CLOSED"
        assert (await current(database, row))["status"] == "FINISHED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ClientCheckIn)
                    .where(ClientCheckIn.series_id == UUID(row["id"]))
                )
                == 2
            )
    finally:
        await database.close()


async def test_pause_stop_fence_inflight_send_and_resume_reuses_child(setup, database_url):
    client, fixture, _ = setup
    row = create_series(client, fixture).json()
    database, worker = runtime(database_url)
    scheduler = SeriesActivities(database)
    try:
        await scheduler.advance_series(series_activation(fixture, row))
        row = await current(database, row)
        task = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{row['active_task_id']}", headers=AUTH
        ).json()
        run = await worker._prepare(activation(task))
        paused = control(client, fixture, row, "PAUSE")
        assert paused.status_code == 200, paused.text
        tool = next(t for t in worker._tools(run) if t.name == "send_client_check_in")
        ctx = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="test", tool_arguments="{}"
        )
        with pytest.raises(StaleExecution):
            await tool.on_invoke_tool(ctx, "{}")
        await worker._commit(run, activation(task), None, {})
        assert (await worker.execute_task(activation(task, 1)))["status"] == "BLOCKED"
        assert control(client, fixture, row, "RESUME", key="stale").status_code == 409
        resumed = control(client, fixture, paused.json(), "RESUME", key="resume")
        assert resumed.status_code == 200
        assert resumed.json()["active_task_id"] == task["id"]
        stopped = control(client, fixture, resumed.json(), "STOP", key="stop")
        assert stopped.status_code == 200
        assert (await worker.execute_task(activation(task, 2)))["status"] == "CANCELLED"
        assert (await scheduler.advance_series(series_activation(fixture, row)))[
            "status"
        ] == "CLOSED"
        assert control(client, fixture, resumed.json(), "STOP", key="stop").json()["replayed"]
        async with database.sessions() as session:
            assert not await session.scalar(
                select(ToolInvocation.id).where(ToolInvocation.task_id == UUID(task["id"]))
            )
    finally:
        await database.close()


async def test_end_date_cancels_only_unsent_occurrence_and_future_work_waits(setup, database_url):
    client, fixture, _ = setup
    row = create_series(
        client, fixture, first_due_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    ).json()
    database, _ = runtime(database_url)
    scheduler = SeriesActivities(database)
    try:
        outcome = await scheduler.advance_series(series_activation(fixture, row))
        assert "wake_at" in outcome and (await current(database, row))["occurrence_count"] == 0
        await due(database, row)
        await scheduler.advance_series(series_activation(fixture, row))
        row = await current(database, row)
        async with database.sessions.begin() as session:
            saved = await session.get(CheckInSeries, UUID(row["id"]))
            saved.ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert (await scheduler.advance_series(series_activation(fixture, row)))[
            "status"
        ] == "CLOSED"
        async with database.sessions() as session:
            assert (await session.get(Task, UUID(row["active_task_id"]))).status == "CANCELLED"
            assert await session.scalar(
                select(OutboxMessage.id).where(OutboxMessage.destination == "temporal.signal_task")
            )
    finally:
        await database.close()


async def test_stop_after_send_still_accepts_reply_and_failed_child_pauses_series(
    setup, database_url
):
    client, fixture, _ = setup
    row = create_series(client, fixture).json()
    database, worker = runtime(database_url)
    scheduler = SeriesActivities(database)
    try:
        await scheduler.advance_series(series_activation(fixture, row))
        row = await current(database, row)
        task = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{row['active_task_id']}", headers=AUTH
        ).json()
        assert (await worker.execute_task(activation(task)))["status"] == "WAITING"
        assert control(client, fixture, row, "STOP").status_code == 200
        assert reply(client, task).status_code == 201
        assert (await worker.execute_task(activation(task, 1)))["status"] == "COMPLETED"
        next_series = create_series(client, fixture, key="new-series").json()
        await scheduler.advance_series(series_activation(fixture, next_series))
        next_series = await current(database, next_series)
        failed = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{next_series['active_task_id']}", headers=AUTH
        ).json()
        await worker.fail_task(activation(failed))
        await scheduler.advance_series(series_activation(fixture, next_series))
        next_series = await current(database, next_series)
        assert next_series["status"] == "PAUSED" and next_series["occurrence_count"] == 1
        assert control(client, fixture, next_series, "RESUME", key="bad-resume").status_code == 409
    finally:
        await database.close()


async def test_series_workflow_recovers_paused_and_runs_two_occurrences_without_overlap(
    setup, database_url
):
    import asyncio
    from uuid import uuid4

    from temporalio.client import Client
    from temporalio.worker import Worker

    from agents_platform.check_in_series import enqueue_series, series_event
    from agents_platform.coordination import SupervisorActivities
    from agents_platform.dispatch import dispatch_once
    from agents_platform.orchestration.workflows import (
        CaseWorkflow,
        CheckInSeriesWorkflow,
        TaskWorkflow,
    )

    client, fixture, _ = setup
    row = create_series(client, fixture).json()
    database, specialist = runtime(database_url)
    scheduler = SeriesActivities(database)
    supervisor = SupervisorActivities(database, specialist.settings)
    temporal = await Client.connect("localhost:7233")
    queue = f"series-{uuid4()}"

    def worker():
        return Worker(
            temporal,
            task_queue=queue,
            workflows=[CheckInSeriesWorkflow, TaskWorkflow, CaseWorkflow],
            activities=[
                scheduler.advance_series,
                scheduler.fail_series,
                specialist.execute_task,
                specialist.fail_task,
                supervisor.coordinate_case,
                supervisor.fail_supervisor,
            ],
        )

    async def pump(predicate):
        async with asyncio.timeout(35):
            while True:
                await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
                if await predicate():
                    return
                await asyncio.sleep(0.1)

    async def active_status(status):
        saved = await current(database, row)
        if not saved["active_task_id"]:
            return False
        async with database.sessions() as session:
            return (await session.get(Task, UUID(saved["active_task_id"]))).status == status

    async def between():
        saved = await current(database, row)
        return saved["occurrence_count"] == 1 and saved["active_task_id"] is None

    async def ended():
        return (await current(database, row))["status"] == "FINISHED"

    try:
        async with worker():
            await pump(lambda: active_status("WAITING"))
            saved = await current(database, row)
            first = (
                await asyncio.to_thread(
                    client.get,
                    f"/v1/firms/{fixture['firm_id']}/tasks/{saved['active_task_id']}",
                    headers=AUTH,
                )
            ).json()
            assert (
                await asyncio.to_thread(reply, client, first, pain_score=9, requests_help=True)
            ).status_code == 201
            await pump(lambda: active_status("BLOCKED"))
        # Persist control and review while no workflow worker is running.
        saved = await current(database, row)
        assert control(client, fixture, saved, "PAUSE").status_code == 200
        first = client.get(path(first), headers=AUTH).json()
        assert decide(client, first).status_code == 200
        async with worker():
            await pump(lambda: active_status("COMPLETED"))
            saved = await current(database, row)
            assert saved["status"] == "PAUSED" and saved["occurrence_count"] == 1
            resumed = await asyncio.to_thread(
                control, client, fixture, saved, "RESUME", "resume-after-review"
            )
            assert resumed.status_code == 200, resumed.text
            await pump(between)
            # Advance only the test's persisted timer, then wake the sleeping workflow.
            async with database.sessions.begin() as session:
                series = await session.get(CheckInSeries, UUID(row["id"]), with_for_update=True)
                series.next_due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                event = await series_event(session, series, "test.timer_advanced", {})
                await enqueue_series(session, series, event)
            await pump(lambda: active_status("WAITING"))
            saved = await current(database, row)
            assert saved["occurrence_count"] == 2 and saved["active_task_id"] != first["id"]
            second = (
                await asyncio.to_thread(
                    client.get,
                    f"/v1/firms/{fixture['firm_id']}/tasks/{saved['active_task_id']}",
                    headers=AUTH,
                )
            ).json()
            assert (await asyncio.to_thread(reply, client, second, pain_score=3)).status_code == 201
            await pump(ended)
        async with database.sessions() as session:
            sends = await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .join(
                    ClientCheckIn,
                    (ClientCheckIn.task_id == ToolInvocation.task_id)
                    & (ClientCheckIn.tenant_id == ToolInvocation.tenant_id),
                )
                .where(
                    ClientCheckIn.series_id == UUID(row["id"]),
                    ToolInvocation.tool_name == "send_client_check_in",
                )
            )
            assert sends == 2
    finally:
        await temporal.get_workflow_handle(
            f"firm/{fixture['firm_id']}/case/{fixture['assignment']['case_id']}/supervisor"
        ).terminate()
        await database.close()


async def test_concurrent_scheduler_ticks_create_one_child_and_end_only_series_is_bounded(
    setup, database_url
):
    import asyncio

    client, fixture, _ = setup
    row = create_series(client, fixture).json()
    database, _ = runtime(database_url)
    scheduler = SeriesActivities(database)
    try:
        await asyncio.gather(
            *(scheduler.advance_series(series_activation(fixture, row)) for _ in range(3))
        )
        row = await current(database, row)
        assert row["occurrence_count"] == 1
        group = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{row['group_task_id']}", headers=AUTH
        ).json()
        assert len(group["check_in_series"]["occurrences"]) == 1
        stopped = control(client, fixture, row, "STOP")
        assert stopped.status_code == 200
        end_only = create_series(
            client,
            fixture,
            key="end-only",
            max_occurrences=None,
            ends_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        assert end_only.status_code == 201, end_only.text
        assert end_only.json()["max_occurrences"] is None
        assert "hard cap 100" in end_only.json()["schedule"]
    finally:
        await database.close()

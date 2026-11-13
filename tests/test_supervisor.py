"""Case coordination tests against disposable PostgreSQL and isolated Temporal queues."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.coordination import SupervisorAction, SupervisorActivities, SupervisorDecision
from agents_platform.db import Database
from agents_platform.execution import TaskActivities
from agents_platform.models import CaseCoordination, Execution, Task

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def body(fixture):
    a = fixture["assignment"]
    return {
        "objective": "Collect records and imaging from the existing hospital.",
        "targets": [
            {k: a[k] for k in ("patient_contact_id", "provider_contact_id", "requirements")}
        ],
    }


def path(fixture):
    return f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/supervisor"


def submit(client, fixture, payload=None, key="objective"):
    return client.post(
        path(fixture), headers={**AUTH, "Idempotency-Key": key}, json=payload or body(fixture)
    )


def activation(fixture, cycle=0, trigger="event"):
    return {
        "tenant_id": fixture["firm_id"],
        "case_id": fixture["assignment"]["case_id"],
        "cycle": cycle,
        "trigger": trigger,
    }


def runtime(database_url):
    database = Database(database_url)
    return database, SupervisorActivities(database, Settings(_env_file=None, agent_mode="scripted"))


def test_objective_api_is_scoped_idempotent_and_requires_explicit_revision(setup):
    client, first, second = setup
    assert client.post(path(first), json=body(first)).status_code in (401, 403)
    response = submit(client, first)
    assert response.status_code == 201, response.text
    assert submit(client, first).json()["replayed"]
    assert submit(client, first, {**body(first), "objective": "Changed"}).status_code == 409
    assert submit(client, first, key="different-key").status_code == 409
    assert (
        client.get(
            path(first).replace(first["firm_id"], second["firm_id"]), headers=AUTH
        ).status_code
        == 404
    )
    invalid = body(second)
    invalid["targets"][0]["patient_contact_id"] = first["assignment"]["patient_contact_id"]
    assert submit(client, second, invalid).status_code == 422
    current = client.get(path(first), headers=AUTH).json()
    assert (
        submit(
            client,
            first,
            {**body(first), "expected_revision": current["revision"]},
            key="explicit-revision",
        ).status_code
        == 201
    )


async def test_supervisor_delegates_once_and_verifies_worker_completion(setup, database_url):
    client, fixture, _ = setup
    response = submit(client, fixture)
    database, supervisor = runtime(database_url)
    worker = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    try:
        first = await supervisor.coordinate_case(activation(fixture))
        assert first["coordination_status"] == "MONITORING"
        assert await supervisor.coordinate_case(activation(fixture)) == first
        async with database.sessions() as session:
            tasks = (
                await session.scalars(
                    select(Task).where(
                        Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT"
                    )
                )
            ).all()
            assert len(tasks) == 1
            task = tasks[0]
            assert task.parent_task_id == UUID(response.json()["group_task_id"])
            assert "Example Hospital" in task.objective
        events = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task.id}/events", headers=AUTH
        ).json()
        assert events[0]["actor_ref"] == "agent/case_supervisor/1"
        run = {
            "tenant_id": fixture["firm_id"],
            "task_id": str(task.id),
            "cycle": 0,
            "trigger": "created",
        }
        assert (await worker.execute_task(run))["status"] == "WAITING"
        received = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task.id}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "ready"},
            json={"status": "RECEIVED", "message": "Simulated records and imaging received"},
        )
        assert received.status_code == 201
        assert (await worker.execute_task({**run, "cycle": 1, "trigger": "event"}))[
            "status"
        ] == "COMPLETED"
        assert (await supervisor.coordinate_case(activation(fixture, 1)))[
            "coordination_status"
        ] == "SATISFIED"
        current = client.get(path(fixture), headers=AUTH).json()
        assert current["status"] == "SATISFIED"
        with connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT status FROM cases WHERE id=%s",
                    (UUID(fixture["assignment"]["case_id"]),),
                ).fetchone()[0]
                == "OPEN"
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE tenant_id=%s AND kind='AGENT'",
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 1
            )
    finally:
        await database.close()


async def test_stale_supervisor_proposal_is_not_applied(setup, database_url, monkeypatch):
    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    original = supervisor._decide

    async def interrupted(run, definition):
        result = await original(run, definition)
        assert post(client, fixture, key="manual-during-plan").status_code == 201
        return result

    try:
        with monkeypatch.context() as patch:
            patch.setattr(supervisor, "_decide", interrupted)
            assert (await supervisor.coordinate_case(activation(fixture)))["status"] == "CONTINUE"
        result = await supervisor.coordinate_case(activation(fixture, 1, "continue"))
        assert result["coordination_status"] == "MONITORING"
        async with database.sessions() as session:
            runs = (
                await session.scalars(
                    select(Execution)
                    .where(Execution.tenant_id == UUID(fixture["firm_id"]))
                    .order_by(Execution.cycle)
                )
            ).all()
            assert runs[0].status == "STALE"
            assert (
                len(
                    (
                        await session.scalars(
                            select(Task).where(
                                Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT"
                            )
                        )
                    ).all()
                )
                == 1
            )
    finally:
        await database.close()


async def test_invalid_batch_has_no_partial_assignment_or_false_completion(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)

    async def invalid(*args):
        return SupervisorDecision(
            summary="Invalid proposal",
            evidence_event_ids=[],
            actions=[
                SupervisorAction(type="create_task", target_index=0, question=None),
                SupervisorAction(type="create_task", target_index=99, question=None),
            ],
            objective_satisfied=True,
            next_review_at=None,
        ), {}

    monkeypatch.setattr(supervisor, "_decide", invalid)
    try:
        result = await supervisor.coordinate_case(activation(fixture))
        assert result["coordination_status"] == "NEEDS_REVIEW"
        with connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE tenant_id=%s AND kind='AGENT'",
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 0
            )
        assert client.get(path(fixture), headers=AUTH).json()["plan"]["rejected"]
    finally:
        await database.close()


async def test_unsupported_scope_requests_review_and_supervisor_mentions_wake(setup, database_url):
    client, fixture, _ = setup
    response = submit(client, fixture, {"objective": "Check in with the client", "targets": []})
    database, supervisor = runtime(database_url)
    try:
        assert (await supervisor.coordinate_case(activation(fixture)))[
            "coordination_status"
        ] == "NEEDS_REVIEW"
        group = response.json()["group_task_id"]
        comment = client.post(
            f"/v1/operator/firms/{fixture['firm_id']}/tasks/{group}/comments",
            headers={**AUTH, "Idempotency-Key": "guidance"},
            json={
                "text": "@supervisor Please explain the missing capability.",
                "mentions": ["supervisor"],
            },
        )
        assert comment.status_code == 201, comment.text
        assert (await supervisor.coordinate_case(activation(fixture, 1)))[
            "coordination_status"
        ] == "NEEDS_REVIEW"
        async with database.sessions() as session:
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(group), Execution.cycle == 1)
            )
            assert any(
                "missing capability" in s["content"]["text"] for s in run.context_json["sources"]
            )
        # A guidance comment cannot pretend to approve or resolve an intervention.
        assert client.get(path(fixture), headers=AUTH).json()["plan"]["reviews"]
    finally:
        await database.close()


async def test_case_and_specialist_workflows_survive_worker_restart(setup, database_url):
    from temporalio.client import Client
    from temporalio.worker import Worker

    from agents_platform.dispatch import dispatch_once
    from agents_platform.orchestration.workflows import CaseWorkflow, TaskWorkflow

    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    specialist = TaskActivities(
        database, Settings(_env_file=None, agent_mode="scripted", demo_follow_up_seconds=60)
    )
    temporal = await Client.connect("localhost:7233")
    queue = f"supervisor-{uuid4()}"

    def worker():
        return Worker(
            temporal,
            task_queue=queue,
            workflows=[CaseWorkflow, TaskWorkflow],
            activities=[
                supervisor.coordinate_case,
                supervisor.fail_supervisor,
                specialist.execute_task,
                specialist.fail_task,
            ],
        )

    async def dispatch_until(predicate):
        for _ in range(300):
            await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
            if await predicate():
                return
            await asyncio.sleep(0.1)
        pytest.fail("Case workflow did not reach expected state")

    async def get_task():
        async with database.sessions() as session:
            return await session.scalar(
                select(Task).where(Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT")
            )

    async def is_waiting():
        task = await get_task()
        return task and task.status == "WAITING"

    async def is_satisfied():
        async with database.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            return coord.status == "SATISFIED"

    try:
        async with worker():
            await dispatch_until(is_waiting)
        task = await get_task()
        result = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task.id}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "offline"},
            json={"status": "RECEIVED", "message": "Simulated response while worker offline"},
        )
        assert result.status_code == 201
        async with worker():
            await dispatch_until(is_satisfied)
        assert await is_satisfied()
    finally:
        coord = client.get(path(fixture), headers=AUTH).json()
        await temporal.get_workflow_handle(coord["workflow_id"]).terminate(
            "Disposable test complete"
        )
        await database.close()


async def test_duplicate_wake_keeps_existing_review_timer(setup, database_url, monkeypatch):
    from datetime import timedelta

    from agents_platform.execution import now

    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    original = supervisor._decide

    async def timed(run, definition):
        decision, usage = await original(run, definition)
        decision.next_review_at = now() + timedelta(minutes=5)
        return decision, usage

    monkeypatch.setattr(supervisor, "_decide", timed)
    try:
        first = await supervisor.coordinate_case(activation(fixture))
        assert first["wake_at"]
        duplicate = await supervisor.coordinate_case(activation(fixture, 1))
        assert duplicate["wake_at"] == first["wake_at"]
    finally:
        await database.close()


async def test_no_action_does_not_leave_uncovered_objective_silent(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)

    async def empty(*args):
        return SupervisorDecision(
            summary="Nothing to do",
            evidence_event_ids=[],
            actions=[],
            objective_satisfied=False,
            next_review_at=None,
        ), {}

    monkeypatch.setattr(supervisor, "_decide", empty)
    try:
        assert (await supervisor.coordinate_case(activation(fixture)))[
            "coordination_status"
        ] == "NEEDS_REVIEW"
        current = client.get(path(fixture), headers=AUTH).json()
        assert current["plan"]["reviews"]
        assert "No assignment" in current["plan"]["reviews"][0]["question"]
    finally:
        await database.close()


async def test_completed_task_without_evidence_cannot_satisfy_case(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    with connect(database_url) as connection:
        connection.execute("UPDATE tasks SET status='COMPLETED' WHERE id=%s", (UUID(task["id"]),))
        connection.execute(
            "UPDATE records_requests SET status='RECEIVED' WHERE task_id=%s", (UUID(task["id"]),)
        )
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    try:
        assert (await supervisor.coordinate_case(activation(fixture)))[
            "coordination_status"
        ] == "NEEDS_REVIEW"
        assert (
            "verified evidence"
            in client.get(path(fixture), headers=AUTH).json()["plan"]["reviews"][0]["question"]
        )
    finally:
        await database.close()


async def test_cancelled_scope_escalates_without_recreating_or_looping(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    with connect(database_url) as connection:
        connection.execute("UPDATE tasks SET status='CANCELLED' WHERE id=%s", (UUID(task["id"]),))
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    try:
        result = await supervisor.coordinate_case(activation(fixture))
        assert result["coordination_status"] == "NEEDS_REVIEW"
        assert result["status"] == "WAITING"
        with connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE tenant_id=%s AND kind='AGENT'",
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 1
            )
    finally:
        await database.close()


async def test_instruction_revision_change_invalidates_inflight_plan(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = runtime(database_url)
    original = supervisor._decide

    async def revise(run, definition):
        result = await original(run, definition)
        with connect(database_url) as connection:
            connection.execute(
                "UPDATE instructions SET revision=revision+1 WHERE tenant_id=%s",
                (UUID(fixture["firm_id"]),),
            )
        return result

    monkeypatch.setattr(supervisor, "_decide", revise)
    try:
        assert (await supervisor.coordinate_case(activation(fixture)))["status"] == "CONTINUE"
        with connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE tenant_id=%s AND kind='AGENT'",
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 0
            )
    finally:
        await database.close()


async def test_comment_and_supervisor_snapshot_do_not_deadlock(setup, database_url, monkeypatch):
    import threading

    import agents_platform.coordination as coordination

    client, fixture, _ = setup
    group = submit(client, fixture).json()["group_task_id"]
    database, supervisor = runtime(database_url)
    preparing, comment_locked = asyncio.Event(), threading.Event()
    original_snapshot = coordination.snapshot
    original_enqueue = coordination.enqueue_supervisor

    async def gated_snapshot(session, coord):
        # The coordinator holds its row lock; let the comment lock its task row.
        preparing.set()
        assert await asyncio.to_thread(comment_locked.wait, 5)
        return await original_snapshot(session, coord)

    async def notify(session, event, **kwargs):
        comment_locked.set()
        return await original_enqueue(session, event, **kwargs)

    monkeypatch.setattr(coordination, "snapshot", gated_snapshot)
    monkeypatch.setattr(coordination, "enqueue_supervisor", notify)
    try:
        pending = asyncio.create_task(supervisor._prepare(activation(fixture)))
        await asyncio.wait_for(preparing.wait(), 5)
        comment = asyncio.create_task(
            asyncio.to_thread(
                client.post,
                f"/v1/operator/firms/{fixture['firm_id']}/tasks/{group}/comments",
                headers={**AUTH, "Idempotency-Key": "concurrent"},
                json={"text": "@supervisor Please review progress.", "mentions": ["supervisor"]},
            )
        )
        prepared, response = await asyncio.wait_for(asyncio.gather(pending, comment), 10)
        assert not isinstance(prepared, dict)
        assert response.status_code == 201, response.text
    finally:
        await database.close()

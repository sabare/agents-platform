"""A human acknowledgement resumes the same task without granting contact permission."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_check_in import activation, create, path, reply, runtime
from test_memory_history import case_change
from test_supervisor import activation as supervisor_activation
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.coordination import SupervisorActivities
from agents_platform.models import (
    ActivityEvent,
    CaseCoordination,
    ClientReport,
    OutboxMessage,
    Source,
    ToolInvocation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


async def blocked(client, fixture, worker, key="review-task"):
    task = create(client, fixture, key=key).json()
    assert (await worker.execute_task(activation(task)))["status"] == "WAITING"
    report = reply(client, task, pain_score=9, requests_help=True, message="I need help today.")
    assert report.status_code == 201, report.text
    assert (await worker.execute_task(activation(task, 1)))["status"] == "BLOCKED"
    return client.get(path(task), headers=AUTH).json()


def decide(client, task, decision="ALLOW_COMPLETION", key="decision-1", **overrides):
    review = task["client_check_in"]["report"]["human_review"]
    return client.post(
        path(task) + f"/client-reviews/{review['id']}/decisions",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "expected_task_version": task["version"],
            "expected_review_version": review["version"],
            "decision": decision,
            "reason": "I reviewed the report and handled the client's request.",
            **overrides,
        },
    )


async def test_acknowledge_resume_complete_and_supervisor_sees_outcome(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        report = task["client_check_in"]["report"]
        assert report["needs_review"] and report["human_review"]["status"] == "OPEN"
        supervisor = SupervisorActivities(database, worker.settings)
        await supervisor.coordinate_case(supervisor_activation(fixture))
        result = decide(client, task)
        assert result.status_code == 200, result.text
        assert result.json()["review"]["decided_by"] == "local-operator"
        assert result.json()["review"]["status"] == "RESOLVED"
        assert (await worker.execute_task(activation(task, 2)))["status"] == "COMPLETED"
        await supervisor.coordinate_case({**supervisor_activation(fixture), "cycle": 1})
        current = client.get(path(task), headers=AUTH).json()
        assert current["id"] == task["id"] and current["workflow_id"] == task["workflow_id"]
        assert current["client_check_in"]["report"]["flagged_for_review"]
        assert not current["client_check_in"]["report"]["needs_review"]
        async with database.sessions() as session:
            assert (await session.get(ClientReport, UUID(report["id"]))).needs_review
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            assert coord.status == "MONITORING" and not coord.plan_json["reviews"]
            outcome = coord.plan_json["client_review_outcomes"][0]
            assert (
                outcome["decision"] == "ALLOW_COMPLETION" and outcome["task_status"] == "COMPLETED"
            )
            sends = await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_client_check_in",
                )
            )
            assert sends == 1
            decisions = (
                await session.scalars(
                    select(ActivityEvent).where(
                        ActivityEvent.task_id == UUID(task["id"]),
                        ActivityEvent.event_type == "client.review_decided",
                    )
                )
            ).all()
            assert len(decisions) == 1
    finally:
        await database.close()


async def test_request_changes_keeps_review_open_then_explicit_acknowledgement_resumes(
    setup, database_url
):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        response = decide(
            client, task, "REQUEST_CHANGES", reason="Explain the reported treatment change."
        )
        assert response.status_code == 200, response.text
        assert response.json()["review"]["status"] == "OPEN"
        fresh = await worker._prepare(activation(task, 2))
        assert (
            fresh.context["domain"]["state"]["check_in"]["report"]["human_review"]["reason"]
            == "Explain the reported treatment change."
        )
        decision, usage = await worker._decide(fresh, worker._tools(fresh))
        assert (await worker._commit(fresh, activation(task, 2), decision, usage))[
            "status"
        ] == "BLOCKED"
        current = client.get(path(task), headers=AUTH).json()
        assert decide(client, current, key="acknowledge-after-changes").status_code == 200
        assert (await worker.execute_task(activation(task, 3)))["status"] == "COMPLETED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Source)
                    .where(
                        Source.tenant_id == UUID(fixture["firm_id"]),
                        Source.kind == "CLIENT_REVIEW_DECISION",
                    )
                )
                == 2
            )
    finally:
        await database.close()


async def test_replay_conflicting_stale_and_invalid_decisions(setup, database_url):
    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        assert decide(client, task, expected_task_version=task["version"] - 1).status_code == 409
        assert decide(client, task, expected_review_version=99).status_code == 409
        for changes in ({"reason": " "}, {"decision": "CLEAR_HOLD"}, {"approved": True}):
            assert decide(client, task, **changes).status_code == 422
        review = task["client_check_in"]["report"]["human_review"]
        url = path(task) + f"/client-reviews/{review['id']}/decisions"
        assert client.post(url, json={}).status_code == 401
        forbidden = url.replace(task["tenant_id"], foreign["firm_id"])
        payload = {
            "decision": "ALLOW_COMPLETION",
            "reason": "Handled",
            "expected_task_version": task["version"],
            "expected_review_version": 1,
        }
        assert (
            client.post(
                forbidden, headers={**AUTH, "Idempotency-Key": "foreign"}, json=payload
            ).status_code
            == 404
        )
        first = decide(client, task)
        assert first.status_code == 200
        assert (await worker.execute_task(activation(task, 2)))["status"] == "COMPLETED"
        replay = decide(client, task)
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert decide(client, task, reason="Different action").status_code == 409
        assert decide(client, task, key="new-decision").status_code == 409
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(
                        ActivityEvent.task_id == UUID(task["id"]),
                        ActivityEvent.event_type == "client.review_decided",
                    )
                )
                == 1
            )
    finally:
        await database.close()


async def test_acknowledgement_does_not_release_contact_hold_or_obligation(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        hold = case_change(client, fixture)
        assert hold.status_code == 200
        task = client.get(path(task), headers=AUTH).json()
        assert decide(client, task).status_code == 200
        assert (await worker.execute_task(activation(task, 2)))["status"] == "BLOCKED"
        current = client.get(path(task), headers=AUTH).json()
        assert current["client_check_in"]["report"]["human_review"]["status"] == "RESOLVED"
        case_memory = client.get(
            f"/v1/firms/{task['tenant_id']}/cases/{task['case_id']}/memory", headers=AUTH
        ).json()
        assert case_memory["restrictions"][0]["status"] == "OPEN"
        # Lift the case hold through its separate explicit API, then add an obligation.
        from test_memory_history import case_path

        release = client.post(
            case_path(fixture) + "/restrictions",
            headers={**AUTH, "Idempotency-Key": "release"},
            json={
                "expected_version": case_memory["case_memory_version"],
                "operation": "RESOLVE",
                "item_id": case_memory["restrictions"][0]["id"],
                "text": "Separately verified contact permission.",
            },
        )
        assert release.status_code == 200, release.text
        current = client.get(path(task), headers=AUTH).json()
        add = client.post(
            path(task) + "/memory",
            headers={**AUTH, "Idempotency-Key": "obligation"},
            json={
                "expected_version": current["version"],
                "kind": "OBLIGATION",
                "text": "Teammate must record a callback.",
            },
        )
        assert add.status_code == 200
        assert (await worker.execute_task(activation(task, 3)))["status"] == "BLOCKED"
        assert (
            "obligations"
            in client.get(path(task), headers=AUTH).json()["progress_json"]["last_reason"]
        )
    finally:
        await database.close()


async def test_concurrent_decisions_have_one_winner_and_one_dispatch(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)

        def send(i):
            return decide(client, task, key=f"concurrent-{i}")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        async with database.sessions() as session:
            event = await session.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.task_id == UUID(task["id"]),
                    ActivityEvent.event_type == "client.review_decided",
                )
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(OutboxMessage)
                    .where(OutboxMessage.event_id == event.id)
                )
                == 2
            )
    finally:
        await database.close()


async def test_changed_report_and_old_supervisor_snapshot_fail_closed(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        supervisor = SupervisorActivities(database, worker.settings)
        prepared, definition = await supervisor._prepare(supervisor_activation(fixture))
        decision, usage = await supervisor._decide(prepared, definition)
        assert decide(client, task).status_code == 200
        assert (await supervisor._commit(prepared, decision, usage))["status"] == "CONTINUE"
        await supervisor.coordinate_case({**supervisor_activation(fixture), "cycle": 1})
        async with database.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            assert not coord.plan_json["reviews"]  # Unread original flag did not reopen it.
        other = await blocked(client, fixture, worker, key="changed-report")
        async with database.sessions.begin() as session:
            source = await session.get(
                Source, UUID(other["client_check_in"]["report"]["source_id"])
            )
            source.content = {"report": {"message": "Corrected report"}}
        assert decide(client, other, key="changed").status_code == 409
    finally:
        await database.close()


async def test_decision_and_dispatch_rollback_together(setup, database_url, monkeypatch):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        from agents_platform.memory_api import wake_task

        async def fail(*args):
            raise RuntimeError("Simulated dispatch persistence failure")

        monkeypatch.setattr("agents_platform.memory_api.wake_task", fail)
        assert decide(client, task).status_code == 500
        current = client.get(path(task), headers=AUTH).json()
        assert current["version"] == task["version"]
        assert current["client_check_in"]["report"]["human_review"]["status"] == "OPEN"
        async with database.sessions() as session:
            assert not await session.scalar(
                select(Source.id).where(
                    Source.tenant_id == UUID(task["tenant_id"]),
                    Source.kind == "CLIENT_REVIEW_DECISION",
                )
            )
        monkeypatch.setattr("agents_platform.memory_api.wake_task", wake_task)
        assert decide(client, task).status_code == 200
    finally:
        await database.close()


async def test_review_wake_survives_worker_restart_and_reaches_supervisor(setup, database_url):
    import asyncio
    from uuid import uuid4

    from temporalio.client import Client
    from temporalio.worker import Worker

    from agents_platform.dispatch import dispatch_once
    from agents_platform.models import Task
    from agents_platform.orchestration.workflows import CaseWorkflow, TaskWorkflow

    client, fixture, _ = setup
    database, specialist = runtime(database_url)
    coordinator = SupervisorActivities(database, specialist.settings)
    temporal = await Client.connect("localhost:7233")
    queue = f"client-review-{uuid4()}"
    task = create(client, fixture).json()

    def worker():
        return Worker(
            temporal,
            task_queue=queue,
            workflows=[CaseWorkflow, TaskWorkflow],
            activities=[
                specialist.execute_task,
                specialist.fail_task,
                coordinator.coordinate_case,
                coordinator.fail_supervisor,
            ],
        )

    async def pump_until(predicate):
        async with asyncio.timeout(35):
            while True:
                await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
                if await predicate():
                    return
                await asyncio.sleep(0.1)

    async def status(wanted):
        async with database.sessions() as session:
            return (await session.get(Task, UUID(task["id"]))).status == wanted

    async def visible_outcome():
        async with database.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            return coord.status == "MONITORING" and any(
                r["task_status"] == "COMPLETED"
                for r in coord.plan_json.get("client_review_outcomes", [])
            )

    try:
        async with worker():
            await pump_until(lambda: status("WAITING"))
            assert (
                await asyncio.to_thread(reply, client, task, pain_score=9, requests_help=True)
            ).status_code == 201
            await pump_until(lambda: status("BLOCKED"))
        # Teammate decides while there is no activity worker or dispatcher running.
        blocked_task = client.get(path(task), headers=AUTH).json()
        assert decide(client, blocked_task).status_code == 200
        async with worker():
            await pump_until(lambda: status("COMPLETED"))
            await pump_until(visible_outcome)
        current = client.get(path(task), headers=AUTH).json()
        assert current["workflow_id"] == task["workflow_id"]
    finally:
        await temporal.get_workflow_handle(
            f"firm/{fixture['firm_id']}/case/{task['case_id']}/supervisor"
        ).terminate()
        await database.close()


async def test_migration_backfills_existing_blocked_report(setup, database_url):
    import subprocess
    import sys

    client, fixture, _ = setup
    database, worker = runtime(database_url)
    task = await blocked(client, fixture, worker)
    await database.close()
    assert "agents_test_" in database_url
    for command in (["downgrade", "a8121096bc69"], ["upgrade", "head"]):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *command],
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    migrated = client.get(path(task), headers=AUTH).json()
    assert migrated["client_check_in"]["report"]["human_review"]["status"] == "OPEN"
    assert decide(client, migrated, key="backfilled-review").status_code == 200

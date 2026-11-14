"""Human decisions survive retries, preserve task identity and never bypass contact holds."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from agents import Runner
from sqlalchemy import func, select
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from test_client_reviews import blocked
from test_memory import activation, change, runtime
from test_records_reminders import create_v3, due
from test_supervisor import activation as case_activation
from test_supervisor import body as objective_body
from test_supervisor import submit
from test_task_persistence import AUTH, task_worker
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.coordination import SupervisorActivities
from agents_platform.dispatch import dispatch_once
from agents_platform.domains import ActionNotDue
from agents_platform.human_request_handlers import backfill_blocked_requests
from agents_platform.models import (
    HumanContinuation,
    HumanDecision,
    HumanRequest,
    OutboxMessage,
    Source,
    Task,
)
from agents_platform.orchestration.workflows import TaskWorkflow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def root(fixture):
    return f"/v1/firms/{fixture['firm_id']}"


def requests(client, fixture, task=None, status="OPEN"):
    response = client.get(
        root(fixture) + "/human-requests",
        headers=AUTH,
        params={"status": status, **({"task_id": task["id"]} if task else {})},
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def response_body(request, action="SCHEDULE_FOLLOW_UP", **changes):
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {
        "expected_task_version": request["task_version"],
        "expected_request_version": request["version"],
        "action": action,
        "reason": "I reviewed the provider history and chose this next step.",
        "payload": {
            "follow_up_at": at.isoformat(),
            "response_due_at": (at + timedelta(days=1)).isoformat(),
        }
        if action == "SCHEDULE_FOLLOW_UP"
        else {},
        **changes,
    }


def decide(client, fixture, request, key="human-decision", body=None, **changes):
    return client.post(
        root(fixture) + f"/human-requests/{request['id']}/decisions",
        headers={**AUTH, "Idempotency-Key": key},
        json=body or response_body(request, **changes),
    )


async def exhausted(client, fixture, worker, database):
    task = create_v3(client, fixture)
    await worker.execute_task(activation(fixture, task))
    for cycle in range(1, 4):
        await due(database, task)
        outcome = await worker.execute_task(activation(fixture, task, cycle))
    assert outcome["status"] == "BLOCKED"
    return task, requests(client, fixture, task)[0]


async def set_due(database, task, *, expired=False):
    async with database.sessions.begin() as session:
        row = await session.scalar(
            select(HumanContinuation).where(
                HumanContinuation.task_id == UUID(task["id"]),
                HumanContinuation.status.in_(["PENDING", "APPLIED"]),
            )
        )
        row.ready_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        if expired:
            row.response_due_at = datetime.now(timezone.utc) - timedelta(seconds=1)


def provider_update(client, fixture, task, status="RECEIVED"):
    response = client.post(
        root(fixture) + f"/tasks/{task['id']}/provider-updates",
        headers={**AUTH, "Idempotency-Key": "new-provider-info"},
        json={"status": status, "message": "New simulated provider information."},
    )
    assert response.status_code == 201, response.text


async def test_records_one_authorized_followup_then_same_task_completes(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, request = await exhausted(client, fixture, worker, database)
        body = response_body(request)
        result = decide(client, fixture, request, body=body)
        assert result.status_code == 200, result.text
        assert result.json()["request"]["status"] == "RESOLVED"
        assert decide(client, fixture, request, body=body).json()["replayed"]
        assert (
            decide(client, fixture, request, body={**body, "reason": "Changed"}).status_code == 409
        )
        outcome = await worker.execute_task(activation(fixture, task, 4))
        assert outcome["status"] == "WAITING"
        assert datetime.fromisoformat(outcome["wake_at"]) == datetime.fromisoformat(
            body["payload"]["follow_up_at"]
        )
        await set_due(database, task)
        assert (await worker.execute_task(activation(fixture, task, 5)))["status"] == "WAITING"
        assert (await worker.execute_task(activation(fixture, task, 5)))["status"] == "WAITING"
        provider_update(client, fixture, task)
        assert (await worker.execute_task(activation(fixture, task, 6)))["status"] == "COMPLETED"
        current = client.get(root(fixture) + f"/tasks/{task['id']}", headers=AUTH).json()
        assert current["workflow_id"] == task["workflow_id"] and current["agent_version"] == 3
        assert current["records_request"]["reminder_count"] == 3
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Source)
                    .where(
                        Source.tenant_id == UUID(fixture["firm_id"]),
                        Source.kind == "SIMULATED_HUMAN_AUTHORIZED_FOLLOW_UP",
                    )
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(HumanDecision)
                    .where(
                        HumanDecision.request_id == UUID(request["id"]),
                    )
                )
                == 1
            )
    finally:
        await database.close()


@pytest.mark.parametrize("interruption", ["hold", "new_information", "early_receipt", "expired"])
async def test_authorized_action_rechecks_current_state_before_send(
    setup, database_url, interruption
):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, request = await exhausted(client, fixture, worker, database)
        assert decide(client, fixture, request).status_code == 200
        if interruption == "hold":
            assert change(client, fixture, task).status_code == 200
        elif interruption in ("new_information", "early_receipt"):
            provider_update(
                client, fixture, task, "RECEIVED" if interruption == "early_receipt" else "PENDING"
            )
        await set_due(database, task, expired=interruption == "expired")
        result = await worker.execute_task(activation(fixture, task, 4))
        assert result["status"] == ("COMPLETED" if interruption == "early_receipt" else "BLOCKED")
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Source)
                    .where(
                        Source.tenant_id == UUID(fixture["firm_id"]),
                        Source.kind == "SIMULATED_HUMAN_AUTHORIZED_FOLLOW_UP",
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(HumanContinuation).where(
                        HumanContinuation.task_id == UUID(task["id"]),
                    )
                )
            ).status == "CANCELLED"
        if interruption != "early_receipt":
            assert requests(client, fixture, task)[0]["id"] != request["id"]
    finally:
        await database.close()


async def test_response_deadline_creates_new_actionable_request_and_stop_is_terminal(
    setup, database_url
):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, request = await exhausted(client, fixture, worker, database)
        assert decide(client, fixture, request).status_code == 200
        await set_due(database, task)
        await worker.execute_task(activation(fixture, task, 4))
        await set_due(database, task, expired=True)
        assert (await worker.execute_task(activation(fixture, task, 5)))["status"] == "BLOCKED"
        current = requests(client, fixture, task)[0]
        assert current["id"] != request["id"] and current["actionable"]
        result = decide(client, fixture, current, action="STOP_COLLECTION")
        assert result.status_code == 200, result.text
        assert (await worker.execute_task(activation(fixture, task, 6)))["status"] == "CANCELLED"
        async with database.sessions() as session:
            assert await session.scalar(
                select(OutboxMessage.id).where(
                    OutboxMessage.tenant_id == UUID(fixture["firm_id"]),
                    OutboxMessage.destination == "temporal.signal_task",
                )
            )
    finally:
        await database.close()


async def test_queue_assignment_auth_scope_and_concurrent_decisions(setup, database_url):
    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    try:
        task, request = await exhausted(client, fixture, worker, database)
        assert not requests(client, foreign)
        url = root(fixture) + f"/human-requests/{request['id']}"
        assert client.get(url).status_code == 401
        assert (
            client.get(
                url.replace(fixture["firm_id"], foreign["firm_id"]), headers=AUTH
            ).status_code
            == 404
        )
        assignment = {
            "expected_request_version": request["version"],
            "assignee": "Case manager",
            "due_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        }
        assigned = client.post(
            url + "/assignment", headers={**AUTH, "Idempotency-Key": "owner"}, json=assignment
        )
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["request"]["overdue"]
        assert assigned.json()["request"]["assignee"] == "Case manager"
        assert client.post(
            url + "/assignment", headers={**AUTH, "Idempotency-Key": "owner"}, json=assignment
        ).json()["replayed"]
        assert decide(client, fixture, request).status_code == 409
        current = requests(client, fixture, task)[0]
        assert decide(client, fixture, current, action="ALLOW_COMPLETION").status_code == 422
        body = response_body(current)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda key: decide(client, fixture, current, key=key, body=body), ["one", "two"]
                )
            )
        assert sorted(r.status_code for r in results) == [200, 409]
    finally:
        await database.close()


async def test_client_review_uses_shared_queue_and_ledger_after_assignment(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = await blocked(client, fixture, worker)
        request = requests(client, fixture, task)[0]
        url = root(fixture) + f"/human-requests/{request['id']}/assignment"
        assert (
            client.post(
                url,
                headers={**AUTH, "Idempotency-Key": "assign"},
                json={
                    "expected_request_version": request["version"],
                    "assignee": "Teammate",
                },
            ).status_code
            == 200
        )
        request = requests(client, fixture, task)[0]
        result = decide(client, fixture, request, action="ALLOW_COMPLETION")
        assert result.status_code == 200, result.text
        assert result.json()["request"]["decisions"][0]["actor"] == "local-operator"
        assert (await worker.execute_task(activation(fixture, task, 2)))["status"] == "COMPLETED"
    finally:
        await database.close()


async def test_supervisor_clarification_updates_approved_scope_and_resumes(setup, database_url):
    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    supervisor = SupervisorActivities(database, worker.settings)
    try:
        assert (
            submit(
                client, fixture, {"objective": "Collect the intended records", "targets": []}
            ).status_code
            == 201
        )
        await supervisor.coordinate_case(case_activation(fixture))
        request = requests(client, fixture)[0]
        assert request["handler"] == "supervisor_objective.v1"
        invalid = objective_body(foreign)
        result = decide(client, fixture, request, action="REVISE_OBJECTIVE", payload=invalid)
        assert result.status_code == 422, result.text
        assert not client.get(
            root(fixture) + f"/human-requests/{request['id']}", headers=AUTH
        ).json()["decisions"]
        result = decide(
            client, fixture, request, action="REVISE_OBJECTIVE", payload=objective_body(fixture)
        )
        assert result.status_code == 200, result.text
        await supervisor.coordinate_case(case_activation(fixture, 1))
        async with database.sessions() as session:
            assert await session.scalar(
                select(Task.id).where(
                    Task.tenant_id == UUID(fixture["firm_id"]),
                    Task.kind == "AGENT",
                )
            )
        assert not requests(client, fixture)
    finally:
        await database.close()


async def test_preupgrade_blockers_are_backfilled_idempotently(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, request = await exhausted(client, fixture, worker, database)
        async with database.sessions.begin() as session:
            row = await session.get(HumanRequest, UUID(request["id"]))
            await session.delete(row)
        await backfill_blocked_requests(database)
        await backfill_blocked_requests(database)
        assert len(requests(client, fixture, task)) == 1
    finally:
        await database.close()


async def test_premature_model_action_returns_to_timer_without_failing(
    setup, database_url, monkeypatch
):
    from test_records_reminders import invoke

    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = create_v3(client, fixture)
        prepared = await worker._prepare(activation(fixture, task))
        tools = {t.name: t for t in worker._tools(prepared)}
        await invoke(tools["send_records_request"], {})
        ready = datetime.now(timezone.utc) + timedelta(seconds=30)

        async def premature(*args, **kwargs):
            try:
                raise ActionNotDue(ready)
            except ActionNotDue as exc:
                raise RuntimeError("SDK-wrapped tool rejection") from exc

        monkeypatch.setattr(Runner, "run", premature)
        decision, usage = await worker._decide(prepared, list(tools.values()))
        assert usage["action_rejected"] == "NOT_DUE"
        outcome = await worker._commit(prepared, activation(fixture, task), decision, usage)
        assert outcome["status"] == "WAITING"
        waiting = await worker._prepare(activation(fixture, task, 1))
        assert isinstance(waiting, dict) and waiting["status"] == "WAITING"
    finally:
        await database.close()


async def test_scheduled_human_action_survives_temporal_worker_restart(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    temporal = await Client.connect("localhost:7233")
    queue = f"human-work-{uuid4()}"
    task = None
    try:
        task, request = await exhausted(client, fixture, worker, database)
        at = datetime.now(timezone.utc) + timedelta(seconds=4)
        body = response_body(
            request,
            payload={
                "follow_up_at": at.isoformat(),
                "response_due_at": (at + timedelta(hours=1)).isoformat(),
            },
        )
        assert decide(client, fixture, request, body=body).status_code == 200
        # Seeded executions 0-3 exhausted automatic reminders. Resume their durable cursor.
        handle = await temporal.start_workflow(
            TaskWorkflow.run,
            activation(fixture, task, 4),
            id=task["workflow_id"],
            task_queue=queue,
        )
        async with task_worker(database, worker.settings, temporal, queue):
            timer_saved = False
            for _ in range(100):
                events = [event async for event in handle.fetch_history_events()]
                timer_saved = any(
                    event.event_type == EventType.EVENT_TYPE_TIMER_STARTED for event in events
                )
                if timer_saved:
                    break
                await asyncio.sleep(0.05)
            assert timer_saved
        await asyncio.sleep(max(0, (at - datetime.now(timezone.utc)).total_seconds()) + 0.2)
        async with task_worker(database, worker.settings, temporal, queue):
            for _ in range(100):
                async with database.sessions() as session:
                    sent = await session.scalar(
                        select(Source.id).where(
                            Source.tenant_id == UUID(fixture["firm_id"]),
                            Source.kind == "SIMULATED_HUMAN_AUTHORIZED_FOLLOW_UP",
                        )
                    )
                if sent:
                    break
                await asyncio.sleep(0.1)
            assert sent
            provider_update(client, fixture, task)
            while await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"])):
                pass
            assert (
                await asyncio.wait_for(
                    temporal.get_workflow_handle(task["workflow_id"]).result(), 30
                )
            )["status"] == "COMPLETED"
    finally:
        if task:
            try:
                await temporal.get_workflow_handle(task["workflow_id"]).terminate()
            except Exception:
                pass
        await database.close()

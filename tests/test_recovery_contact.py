"""Same-assignment recovery, saved effects, scoped contact and generation fencing."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from temporalio.client import Client
from test_memory import activation, change, runtime
from test_records_reminders import invoke
from test_task_persistence import AUTH, post, task_worker, wait_for_status
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import ExecutionIncident, ManualContact, Source, Task, ToolInvocation
from agents_platform.recovery import backfill_incidents

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def root(fixture):
    return f"/v1/firms/{fixture['firm_id']}"


def contacts_body(fixture, **extra):
    return {
        "contact_id": fixture["assignment"]["provider_contact_id"],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "channel": "PHONE",
        "outcome": "REACHED",
        "notes": "Provider confirms remaining items will arrive Friday.",
        **extra,
    }


def incidents(client, fixture):
    response = client.get(root(fixture) + "/incidents", headers=AUTH)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def decision(incident, **extra):
    return {
        "expected_task_version": incident["task_version"],
        "expected_incident_version": incident["version"],
        "action": "RETRY",
        "reason": "Service restored; reviewed saved actions.",
        **extra,
    }


def resolve(client, fixture, incident, body=None, key="recover"):
    return client.post(
        root(fixture) + f"/incidents/{incident['id']}/resolve",
        headers={**AUTH, "Idempotency-Key": key},
        json=body or decision(incident),
    )


def fresh(client, fixture, task):
    return client.get(root(fixture) + f"/tasks/{task['id']}", headers=AUTH).json()


def next_activation(task):
    return {
        "tenant_id": task["tenant_id"],
        "task_id": task["id"],
        "cycle": task["progress_json"]["recovery_cycle"],
        "trigger": "recovery",
        "generation": task["progress_json"]["recovery_generation"],
    }


async def failed_after_send(client, fixture, worker):
    task = post(client, fixture, body={**fixture["assignment"], "agent_version": 3}).json()
    run = await worker._prepare(activation(fixture, task))
    tool = next(t for t in worker._tools(run) if t.name == "send_records_request")
    await invoke(tool, {})
    await worker.fail_task({**activation(fixture, task), "failure_type": "ConnectionError"})
    return task, incidents(client, fixture)[0]


async def test_recovery_retains_saved_send_and_manual_facts_with_fresh_context(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, fixture, worker)
        assert incident["category"] == "SERVICE_FAILURE"
        assert incident["saved_actions"][0]["logical_key"] == "initial-send"
        contact = contacts_body(fixture)
        body = decision(incident, contact=contact)
        result = resolve(client, fixture, incident, body)
        assert result.status_code == 200, result.text
        assert resolve(client, fixture, incident, body).json()["replayed"]
        assert (
            resolve(client, fixture, incident, {**body, "reason": "different"}).status_code == 409
        )
        current = fresh(client, fixture, task)
        assert current["id"] == task["id"] and current["workflow_id"] != task["workflow_id"]
        assert current["agent_version"] == task["agent_version"]
        new = next_activation(current)
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "CANCELLED"
        await worker.fail_task(activation(fixture, task))
        assert fresh(client, fixture, task)["status"] == "PENDING"
        # Deliver remaining evidence and inspect the entirely fresh activation.
        response = client.post(
            root(fixture) + f"/tasks/{task['id']}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "received"},
            json={"status": "RECEIVED", "message": "Synthetic records delivered."},
        )
        assert response.status_code == 201
        prepared = await worker._prepare(new)
        assert any(
            s["content"].get("contact", {}).get("notes") == contact["notes"]
            for s in prepared.context["sources"]
        )
        result, usage = await worker._decide(prepared, worker._tools(prepared))
        assert (await worker._commit(prepared, new, result, usage))["status"] == "COMPLETED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.logical_key == "initial-send",
                    )
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ManualContact)
                    .where(ManualContact.task_id == UUID(task["id"]))
                )
                == 1
            )
    finally:
        await database.close()


async def test_scope_stale_concurrent_bounded_and_cancel_recovery(setup, database_url):
    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, fixture, worker)
        assert client.get(root(fixture) + "/incidents").status_code == 401
        assert incidents(client, foreign) == []
        assert resolve(client, foreign, incident).status_code == 404
        assert (
            resolve(
                client, fixture, incident, decision(incident, expected_task_version=99)
            ).status_code
            == 409
        )
        body = decision(incident)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda k: resolve(client, fixture, incident, body, k), ["one", "two"])
            )
        assert sorted(r.status_code for r in results) == [200, 409]
        for _ in range(3):
            current = fresh(client, fixture, task)
            await worker.fail_task(next_activation(current))
            incident = incidents(client, fixture)[0]
            if incident["generation"] < 3:
                assert (
                    resolve(
                        client, fixture, incident, key=f"retry-{incident['generation']}"
                    ).status_code
                    == 200
                )
        assert incident["generation"] == 3 and incident["allowed_actions"] == ["CANCEL"]
        assert resolve(client, fixture, incident, key="limit").status_code == 409
        result = resolve(
            client, fixture, incident, decision(incident, action="CANCEL"), key="cancel"
        )
        assert result.status_code == 200, result.text
        assert fresh(client, fixture, task)["status"] == "CANCELLED"
    finally:
        await database.close()


async def test_manual_contact_only_on_failed_task_does_not_resume_and_is_idempotent(
    setup, database_url
):
    client, fixture, foreign = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, fixture, worker)
        current = fresh(client, fixture, task)
        url = root(fixture) + f"/tasks/{task['id']}/manual-contacts"
        body = {"expected_task_version": current["version"], "contact": contacts_body(fixture)}
        bad = {**body, "contact": contacts_body(foreign)}
        assert (
            client.post(url, headers={**AUTH, "Idempotency-Key": "bad"}, json=bad).status_code
            == 422
        )
        result = client.post(url, headers={**AUTH, "Idempotency-Key": "contact"}, json=body)
        assert result.status_code == 200, result.text
        assert client.post(url, headers={**AUTH, "Idempotency-Key": "contact"}, json=body).json()[
            "replayed"
        ]
        assert fresh(client, fixture, task)["status"] == "FAILED"
        assert resolve(client, fixture, incident).status_code == 409
        refreshed = incidents(client, fixture)[0]
        assert resolve(client, fixture, refreshed).status_code == 200
        assert client.get(url, headers=AUTH).json()["items"][0]["actor"] == "local-operator"
    finally:
        await database.close()


async def test_manual_provider_followup_is_explicit_and_holds_still_block(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, fixture, worker)
        at = datetime.now(timezone.utc) + timedelta(hours=1)
        contact = contacts_body(
            fixture,
            follow_up_at=at.isoformat(),
            response_due_at=(at + timedelta(days=1)).isoformat(),
        )
        result = resolve(client, fixture, incident, decision(incident, contact=contact))
        assert result.status_code == 200, result.text
        current = fresh(client, fixture, task)
        waiting = await worker.execute_task(next_activation(current))
        assert waiting["status"] == "WAITING" and datetime.fromisoformat(waiting["wake_at"]) == at
        assert change(client, fixture, current).status_code == 200
        # A policy change cancels the authorized action, never clears the hold.
        assert (await worker.execute_task({**next_activation(current), "cycle": 2}))[
            "status"
        ] == "BLOCKED"
        async with database.sessions() as session:
            assert not await session.scalar(
                select(Source.id).where(
                    Source.kind == "SIMULATED_HUMAN_AUTHORIZED_FOLLOW_UP",
                    Source.tenant_id == UUID(fixture["firm_id"]),
                )
            )
    finally:
        await database.close()


async def test_backfill_legacy_failures_and_unknown_delivery_are_not_blindly_retried(
    setup, database_url
):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = post(client, fixture).json()
        async with database.sessions.begin() as session:
            row = await session.get(Task, UUID(task["id"]))
            row.status = "FAILED"
        await backfill_incidents(database)
        await backfill_incidents(database)
        incident = incidents(client, fixture)[0]
        assert len(incidents(client, fixture)) == 1
        async with database.sessions.begin() as session:
            row = await session.get(ExecutionIncident, UUID(incident["id"]))
            row.category = "OUTCOME_UNKNOWN"
        assert resolve(client, fixture, incident).status_code == 409
    finally:
        await database.close()


async def test_temporal_failure_recovery_new_workflow_and_old_outbox_cannot_repeat_send(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    temporal = await Client.connect("localhost:7233")
    queue = f"recover-{uuid4()}"
    task = post(client, fixture, body={**fixture["assignment"], "agent_version": 3}).json()
    original = TaskActivities._commit

    async def fail_first_generation(self, run, activation, decision, usage):
        if not activation.get("generation"):
            raise RuntimeError("Simulated failure after saved tool effects")
        return await original(self, run, activation, decision, usage)

    monkeypatch.setattr(TaskActivities, "_commit", fail_first_generation)
    try:
        async with task_worker(database, worker.settings, temporal, queue):
            await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
            assert await wait_for_status(database, task["id"], {"FAILED"}) == "FAILED"
        incident = incidents(client, fixture)[0]
        assert resolve(client, fixture, incident).status_code == 200
        current = fresh(client, fixture, task)
        response = client.post(
            root(fixture) + f"/tasks/{task['id']}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "received"},
            json={"status": "RECEIVED", "message": "Records arrived."},
        )
        assert response.status_code == 201
        async with task_worker(database, worker.settings, temporal, queue):
            while await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"])):
                pass
            assert (
                await wait_for_status(database, task["id"], {"COMPLETED", "FAILED"}) == "COMPLETED"
            )
            assert (
                await asyncio.wait_for(
                    temporal.get_workflow_handle(current["workflow_id"]).result(), 30
                )
            )["status"] == "COMPLETED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.logical_key == "initial-send",
                    )
                )
                == 1
            )
    finally:
        await database.close()


@pytest.mark.parametrize(
    "outcome,expected",
    [("REACHED", "RECENT_CONTACT"), ("VOICEMAIL", "ELIGIBLE"), ("NO_ANSWER", "ELIGIBLE")],
)
async def test_manual_client_contact_spacing_does_not_fabricate_a_reply(
    setup, database_url, outcome, expected
):
    from test_monthly_check_ins import create as create_monthly

    from agents_platform.check_in_contact import assess_contact
    from agents_platform.models import ClientReport

    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = post(client, fixture).json()
        assert create_monthly(client, fixture).status_code == 201
        body = {
            "expected_task_version": task["version"],
            "contact": contacts_body(
                fixture, contact_id=fixture["assignment"]["patient_contact_id"], outcome=outcome
            ),
        }
        url = root(fixture) + f"/tasks/{task['id']}/manual-contacts"
        response = client.post(url, headers={**AUTH, "Idempotency-Key": "contact"}, json=body)
        assert response.status_code == 200, response.text
        async with database.sessions() as session:
            assessment = await assess_contact(
                session,
                UUID(fixture["firm_id"]),
                UUID(fixture["assignment"]["case_id"]),
                UUID(fixture["assignment"]["patient_contact_id"]),
            )
            assert assessment["code"] == expected
            assert not await session.scalar(
                select(ClientReport.id).where(ClientReport.tenant_id == UUID(fixture["firm_id"]))
            )
    finally:
        await database.close()


async def test_closed_case_blocks_retry_but_still_allows_incident_cancellation(setup, database_url):
    from agents_platform.models import Case

    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, fixture, worker)
        async with database.sessions.begin() as session:
            case = await session.get(Case, UUID(task["case_id"]))
            case.status = "CLOSED"
        assert resolve(client, fixture, incident).status_code == 409
        cancelled = resolve(client, fixture, incident, decision(incident, action="CANCEL"))
        assert cancelled.status_code == 200, cancelled.text
        assert fresh(client, fixture, task)["status"] == "CANCELLED"
    finally:
        await database.close()

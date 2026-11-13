"""Mandatory memory survives time/history limits and fences in-flight actions."""

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from agents.tool_context import ToolContext
from sqlalchemy import func, select
from test_supervisor import activation as supervisor_activation
from test_supervisor import submit
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.execution import StaleExecution, TaskActivities
from agents_platform.models import Execution, ToolInvocation

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def path(fixture, task):
    return f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}"


def change(client, fixture, task, key="hold", **overrides):
    current = client.get(path(fixture, task), headers=AUTH).json()
    payload = {
        "expected_version": current["version"],
        "kind": "STOP_CONTACT",
        "text": "Client withdrew permission to contact this provider.",
        **overrides,
    }
    return client.post(
        path(fixture, task) + "/memory", headers={**AUTH, "Idempotency-Key": key}, json=payload
    )


def runtime(database_url):
    database = Database(database_url)
    return database, TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))


def activation(fixture, task, cycle=0):
    return {
        "tenant_id": fixture["firm_id"],
        "task_id": task["id"],
        "cycle": cycle,
        "trigger": "continue",
    }


def test_memory_writes_are_authenticated_scoped_versioned_and_idempotent(setup):
    client, fixture, other = setup
    task = post(client, fixture).json()
    url = path(fixture, task) + "/memory"
    assert client.get(url).status_code in (401, 403)
    assert (
        client.get(url.replace(fixture["firm_id"], other["firm_id"]), headers=AUTH).status_code
        == 404
    )
    payload = {"expected_version": task["version"], "kind": "STOP_CONTACT", "text": "No contact"}
    headers = {**AUTH, "Idempotency-Key": "once"}
    first = client.post(url, headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert client.post(url, headers=headers, json=payload).json()["replayed"]
    assert (
        client.post(url, headers=headers, json={**payload, "text": "Different"}).status_code == 409
    )
    assert (
        client.post(url, headers={**AUTH, "Idempotency-Key": "stale"}, json=payload).status_code
        == 409
    )
    read = client.get(url, headers=AUTH).json()
    assert len(read["operational_items"]) == 1
    assert read["manifest"]["source_ids"] == [first.json()["source_id"]]
    assert read["task_version"] == task["version"] + 1


async def test_old_hold_survives_restart_and_recent_history_window(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    hold = change(client, fixture, task).json()
    with connect(database_url) as connection:
        # Simulate nine months of elapsed history; mandatory memory is not time-decayed.
        connection.execute(
            "UPDATE sources SET created_at=now()-interval '9 months' WHERE id=%s",
            (UUID(hold["source_id"]),),
        )
        for i in range(15):
            connection.execute(
                """INSERT INTO activity_events
                (id,tenant_id,case_id,task_id,event_type,producer,dedupe_key,actor_ref,payload)
                VALUES (%s,%s,%s,%s,'demo.history','test',%s,'test','{}')""",
                (
                    uuid4(),
                    UUID(fixture["firm_id"]),
                    UUID(task["case_id"]),
                    UUID(task["id"]),
                    f"history/{i}",
                ),
            )
    database, worker = runtime(database_url)
    await database.close()
    database, worker = runtime(database_url)  # Fresh process-equivalent database client.
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "BLOCKED"
        async with database.sessions() as session:
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]))
            )
            assert len(run.context_json["recent_events"]) == 10
            assert not any(e["type"] == "memory.changed" for e in run.context_json["recent_events"])
            assert (
                run.context_json["memory"]["operational_items"][0]["source_id"] == hold["source_id"]
            )
            assert not run.usage_json  # Backend rule, no model call.
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.task_id == UUID(task["id"]))
                )
                == 0
            )
    finally:
        await database.close()


async def test_new_hold_fences_inflight_send_then_explicit_resolution_resumes_same_task(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        stale = await worker._prepare(activation(fixture, task))
        hold = change(client, fixture, task).json()
        send = next(t for t in worker._tools(stale) if t.name == "send_records_request")
        with pytest.raises(StaleExecution):
            await send.on_invoke_tool(
                ToolContext(
                    context=None, tool_name=send.name, tool_call_id="test", tool_arguments="{}"
                ),
                "{}",
            )
        assert (await worker._commit(stale, activation(fixture, task), None, {}))[
            "status"
        ] == "CONTINUE"
        assert (await worker.execute_task(activation(fixture, task, 1)))["status"] == "BLOCKED"
        resolved = change(
            client,
            fixture,
            task,
            key="resolve",
            operation="RESOLVE",
            kind=None,
            item_id=hold["item_id"],
            text="Operator verified renewed permission.",
        )
        assert resolved.status_code == 200, resolved.text
        assert (await worker.execute_task(activation(fixture, task, 2)))["status"] == "WAITING"
        memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
        assert not memory["operational_items"]
        assert memory["history"][0]["resolution_source_id"] == resolved.json()["source_id"]
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_records_request",
                    )
                )
                == 1
            )
    finally:
        await database.close()


async def test_policy_publication_preserves_history_and_invalidates_running_activation(
    setup, database_url
):
    client, fixture, other = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        stale = await worker._prepare(activation(fixture, task))
        original = next(i for i in stale.context["instructions"] if i["scope"] == "FIRM_PROVIDER")
        url = f"/v1/firms/{fixture['firm_id']}/instructions/{original['id']}/revisions"
        payload = {
            "expected_revision": 1,
            "text": "Include the existing request reference in every follow-up.",
            "reason": "Firm approved revised procedure",
        }
        assert (
            client.post(
                url.replace(fixture["firm_id"], other["firm_id"]), headers=AUTH, json=payload
            ).status_code
            == 404
        )
        published = client.post(url, headers=AUTH, json=payload)
        assert published.status_code == 200, published.text
        assert client.post(url, headers=AUTH, json=payload).status_code == 409
        send = next(t for t in worker._tools(stale) if t.name == "send_records_request")
        with pytest.raises(StaleExecution):
            await send.on_invoke_tool(
                ToolContext(
                    context=None, tool_name=send.name, tool_call_id="test", tool_arguments="{}"
                ),
                "{}",
            )
        history = client.get(url, headers=AUTH).json()
        assert [i["revision"] for i in history["history"]] == [1, 2]
        assert history["history"][0]["text"] == original["text"]
        assert history["current"]["text"] == payload["text"]
        with connect(database_url) as connection:
            with pytest.raises(psycopg.errors.RaiseException):
                connection.execute(
                    "UPDATE instruction_revisions SET text='overwritten' WHERE instruction_id=%s",
                    (UUID(original["id"]),),
                )
            connection.rollback()
        await worker._commit(stale, activation(fixture, task), None, {})
        fresh = await worker._prepare(activation(fixture, task, 1))
        assert (
            next(i for i in fresh.context["instructions"] if i["id"] == original["id"])["revision"]
            == 2
        )
        assert stale.context["instructions"] != fresh.context["instructions"]
    finally:
        await database.close()


async def test_supervisor_receives_same_mandatory_memory_and_rejects_stale_plan(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    assert submit(client, fixture).status_code == 201
    database, _ = runtime(database_url)
    supervisor = SupervisorActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    try:
        run, definition = await supervisor._prepare(supervisor_activation(fixture))
        decision, usage = await supervisor._decide(run, definition)
        hold = change(client, fixture, task).json()
        assert (await supervisor._commit(run, decision, usage))["status"] == "CONTINUE"
        fresh, _ = await supervisor._prepare(supervisor_activation(fixture, 1))
        assert fresh.context_json["memory"]["operational_items"][0]["id"] == hold["item_id"]
        assert fresh.context_json["memory"]["manifest"]["complete"]
    finally:
        await database.close()


async def test_open_obligation_prevents_completion_until_explicit_resolution(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    obligation = change(
        client, fixture, task, kind="OBLIGATION", text="Have a teammate verify the imaging export."
    ).json()
    received = client.post(
        path(fixture, task) + "/provider-updates",
        headers={**AUTH, "Idempotency-Key": "received"},
        json={"status": "RECEIVED", "message": "Simulated records and imaging received"},
    )
    assert received.status_code == 201
    database, worker = runtime(database_url)
    try:
        blocked = await worker.execute_task(activation(fixture, task))
        assert blocked["status"] == "BLOCKED" and "obligation" in blocked["reason"]
        assert (
            change(
                client,
                fixture,
                task,
                key="verified",
                operation="RESOLVE",
                kind=None,
                item_id=obligation["item_id"],
                text="Imaging export verified.",
            ).status_code
            == 200
        )
        assert (await worker.execute_task(activation(fixture, task, 1)))["status"] == "COMPLETED"
    finally:
        await database.close()

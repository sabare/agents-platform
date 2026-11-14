import json
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_memory import activation, runtime
from test_records_reminders import create_v3
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.models import (
    ActivityEvent,
    HumanRequest,
    OutboxMessage,
    RecordsRequest,
    Task,
    TaskInbox,
)
from agents_platform.operations import snapshot
from agents_platform.telemetry import JsonFormatter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def endpoint(fixture):
    return f"/v1/operator/firms/{fixture['firm_id']}/operations"


async def test_metrics_are_tenant_scoped_and_exclude_future_and_blocked_work(setup, database_url):
    client, fixture, foreign = setup
    tasks = [create_v3(client, fixture, key=f"ops-{i}") for i in range(5)]
    create_v3(client, foreign)
    at = datetime.now(timezone.utc)
    old = at - timedelta(hours=3)
    database, _ = runtime(database_url)
    try:
        async with database.sessions.begin() as session:
            for index, data in enumerate(tasks):
                task = await session.get(Task, UUID(data["id"]))
                task.created_at = old
                task.status = ["PENDING", "WAITING", "BLOCKED", "FAILED", "WAITING"][index]
                request = await session.scalar(
                    select(RecordsRequest).where(RecordsRequest.task_id == task.id)
                )
                if index in (1, 2, 4):
                    request.callback_at = at + timedelta(days=2) if index == 1 else old
                event = await session.scalar(
                    select(ActivityEvent).where(ActivityEvent.task_id == task.id)
                )
                session.add(
                    TaskInbox(
                        tenant_id=task.tenant_id,
                        case_id=task.case_id,
                        task_id=task.id,
                        event_id=event.id,
                        created_at=old,
                    )
                )
                outbox = await session.scalar(
                    select(OutboxMessage).where(OutboxMessage.event_id == event.id)
                )
                outbox.created_at = old
                outbox.available_at = at + timedelta(hours=1)  # Retry backoff still counts.
                if index == 3:
                    outbox.status = "DELIVERED"
            session.add(
                HumanRequest(
                    tenant_id=UUID(fixture["firm_id"]),
                    case_id=UUID(fixture["assignment"]["case_id"]),
                    task_id=UUID(tasks[2]["id"]),
                    kind="REVIEW",
                    handler="records",
                    subject_id=uuid4(),
                    subject_fingerprint="ops",
                    question="Private question",
                    context_summary="Private summary",
                    evidence_source_ids=[],
                    allowed_actions=[],
                    status="OPEN",
                    assignee="local-operator",
                    due_at=old,
                    dedupe_key="ops-human",
                    created_at=at,
                )
            )
        async with database.sessions() as session:
            result = await snapshot(session, UUID(fixture["firm_id"]), Settings(_env_file=None), at)
        signals = {s["key"]: s for s in result["signals"]}
        assert result["alert_count"] == 5
        assert signals["outbox"]["count"] == 4 and signals["outbox"]["age_seconds"] == 10800
        assert signals["worker"]["count"] == 2  # Initial pending + overdue runnable callback.
        assert signals["failed"]["count"] == 1
        assert (
            signals["callbacks"]["count"] == 2
        )  # Includes blocked callbacks: humans must see them.
        assert signals["human"]["count"] == signals["human"]["overdue_count"] == 1
        assert "Private question" not in json.dumps(result)
        assert all(s["alert"] for s in signals.values())
        api = client.get(endpoint(fixture), headers=AUTH)
        assert api.status_code == 200, api.text
        assert api.headers["X-Request-ID"]
        assert api.json()["firm_id"] == fixture["firm_id"]
        assert client.get(endpoint(foreign), headers=AUTH).json()["signals"][0]["count"] == 1
        assert client.get(endpoint(fixture)).status_code in (401, 403)
        assert (
            client.get(f"/v1/operator/firms/{uuid4()}/operations", headers=AUTH).status_code == 404
        )
    finally:
        await database.close()


async def test_grace_period_thresholds_and_bounded_samples(setup, database_url):
    client, fixture, _ = setup
    tasks = [create_v3(client, fixture, key=f"many-{i}") for i in range(12)]
    database, _ = runtime(database_url)
    at = datetime.now(timezone.utc)
    try:
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(tasks[0]["id"]))
            )
            request.callback_at = at - timedelta(seconds=10)
        async with database.sessions() as session:
            result = await snapshot(session, UUID(fixture["firm_id"]), Settings(_env_file=None), at)
        signals = {s["key"]: s for s in result["signals"]}
        assert signals["outbox"]["count"] == 12 and len(signals["outbox"]["items"]) == 10
        assert not signals["outbox"]["alert"] and not signals["worker"]["alert"]
        assert signals["callbacks"]["count"] == 0
    finally:
        await database.close()


async def test_committed_logs_join_ids_and_rollbacks_emit_no_business_changes(
    setup, database_url, caplog
):
    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    try:
        with caplog.at_level(logging.INFO, logger="agents_platform.telemetry"):
            await worker.execute_task(activation(fixture, task))
        rows = [
            json.loads(JsonFormatter().format(r))
            for r in caplog.records
            if r.name == "agents_platform.telemetry"
        ]
        committed = [
            r
            for r in rows
            if r["message"] in ("execution.committed", "tool.committed", "event.committed")
        ]
        assert committed
        assert all(str(r["firm_id"]) == fixture["firm_id"] for r in committed)
        assert all(str(r["task_id"]) == task["id"] for r in committed)
        assert all(str(r["case_id"]) == fixture["assignment"]["case_id"] for r in committed)
        assert all(r["workflow_id"] == task["workflow_id"] for r in committed)
        assert any(r["execution_id"] for r in committed)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agents_platform.telemetry"):
            async with database.sessions() as session:
                session.add(
                    ActivityEvent(
                        tenant_id=UUID(fixture["firm_id"]),
                        case_id=UUID(task["case_id"]),
                        task_id=UUID(task["id"]),
                        event_type="test.rollback",
                        producer="test",
                        dedupe_key=str(uuid4()),
                        actor_ref="test",
                        payload={"text": "private"},
                    )
                )
                await session.flush()
                await session.rollback()
        assert not [r for r in caplog.records if r.getMessage() == "event.committed"]
    finally:
        await database.close()


async def test_failed_supervisor_is_visible_even_if_group_task_is_pending(setup, database_url):
    from test_supervisor import submit

    from agents_platform.models import CaseCoordination

    client, fixture, _ = setup
    assert submit(client, fixture).status_code == 201
    database, _ = runtime(database_url)
    try:
        async with database.sessions.begin() as session:
            supervisor = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            supervisor.status = "FAILED"
            group_id = supervisor.group_task_id
        result = client.get(endpoint(fixture), headers=AUTH)
        assert result.status_code == 200, result.text
        signal = next(s for s in result.json()["signals"] if s["key"] == "failed")
        assert signal["count"] == 1 and signal["alert"]
        assert signal["items"][0]["task_id"] == str(group_id)
        assert signal["items"][0]["status"] == "FAILED"
    finally:
        await database.close()


async def test_intentional_reminder_and_response_waits_are_not_worker_backlog(setup, database_url):
    from agents_platform.models import HumanContinuation

    client, fixture, _ = setup
    task = create_v3(client, fixture)
    database, worker = runtime(database_url)
    at = datetime.now(timezone.utc)
    try:
        await worker.execute_task(activation(fixture, task))
        async with database.sessions.begin() as session:
            stored = await session.get(Task, UUID(task["id"]))
            event = await session.scalar(
                select(ActivityEvent).where(ActivityEvent.task_id == stored.id)
            )
            session.add(
                TaskInbox(
                    tenant_id=stored.tenant_id,
                    case_id=stored.case_id,
                    task_id=stored.id,
                    event_id=event.id,
                    created_at=at - timedelta(hours=1),
                )
            )
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == stored.id)
            )
            request.next_reminder_at = at + timedelta(hours=1)
        async with database.sessions() as session:
            result = await snapshot(session, UUID(fixture["firm_id"]), Settings(_env_file=None), at)
        assert next(s for s in result["signals"] if s["key"] == "worker")["count"] == 0
        current = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}", headers=AUTH
        ).json()
        contact = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/manual-contacts",
            headers={**AUTH, "Idempotency-Key": "future-followup"},
            json={
                "expected_task_version": current["version"],
                "contact": {
                    "contact_id": fixture["assignment"]["provider_contact_id"],
                    "occurred_at": at.isoformat(),
                    "outcome": "REACHED",
                    "notes": "Agreed appointment",
                    "follow_up_at": (at + timedelta(hours=2)).isoformat(),
                    "response_due_at": (at + timedelta(days=1)).isoformat(),
                },
            },
        )
        assert contact.status_code == 200, contact.text
        async with database.sessions.begin() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            request.next_reminder_at = None
            continuation = await session.scalar(
                select(HumanContinuation).where(HumanContinuation.task_id == UUID(task["id"]))
            )
            continuation.status = "APPLIED"
            continuation.ready_at = at - timedelta(hours=1)
        async with database.sessions() as session:
            result = await snapshot(session, UUID(fixture["firm_id"]), Settings(_env_file=None), at)
        assert next(s for s in result["signals"] if s["key"] == "worker")["count"] == 0
        # Complete evidence must wake processing immediately, even during the response wait.
        received = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates",
            headers={**AUTH, "Idempotency-Key": "received"},
            json={"status": "RECEIVED", "message": "Synthetic records delivered"},
        )
        assert received.status_code == 201, received.text
        async with database.sessions() as session:
            result = await snapshot(session, UUID(fixture["firm_id"]), Settings(_env_file=None), at)
        assert next(s for s in result["signals"] if s["key"] == "worker")["count"] == 1
    finally:
        await database.close()

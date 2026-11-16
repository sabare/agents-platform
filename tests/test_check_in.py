"""Scheduled second domain, attributed comparisons and durable supervisor updates."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from temporalio.client import Client
from test_task_persistence import AUTH, task_worker, wait_for_status
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.check_in_api import ClientReply, compare_report
from agents_platform.client_check_in import CheckInDecision
from agents_platform.config import Settings
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import (
    CaseCoordination,
    ClientReport,
    Execution,
    ToolInvocation,
)
from agents_platform.task_contracts import NextAction


def body(fixture, scheduled_at=None):
    return {
        "case_id": fixture["assignment"]["case_id"],
        "client_contact_id": fixture["assignment"]["patient_contact_id"],
        "scheduled_at": (scheduled_at or datetime.now(timezone.utc)).isoformat(),
    }


def create(client, fixture, key="check-in", scheduled_at=None, **changes):
    return client.post(
        f"/v1/firms/{fixture['firm_id']}/tasks/client-check-in",
        headers={**AUTH, "Idempotency-Key": key},
        json={**body(fixture, scheduled_at), **changes},
    )


def path(task):
    return f"/v1/firms/{task['tenant_id']}/tasks/{task['id']}"


def reply(client, task, key="reply", **changes):
    return client.post(
        path(task) + "/client-replies",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "message": "I feel about the same.",
            "pain_score": 3,
            "daily_function": "UNCHANGED",
            **changes,
        },
    )


def activation(task, cycle=0):
    return {
        "tenant_id": task["tenant_id"],
        "task_id": task["id"],
        "cycle": cycle,
        "trigger": "continue",
    }


def runtime(url):
    db = Database(url)
    return db, TaskActivities(
        db, Settings(_env_file=None, agent_mode="scripted", demo_follow_up_seconds=60)
    )


integration = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def test_comparison_is_explicit_attributed_and_does_not_diagnose():
    baseline = ClientReply(message="Fine", pain_score=3, daily_function="UNCHANGED")
    previous = type("Previous", (), {"report": baseline.model_dump(), "source_id": uuid4()})()
    changes, material, sensitive = compare_report(baseline, previous)
    assert not material and not sensitive and changes["pain_delta"] == 0
    changes, material, sensitive = compare_report(
        ClientReply(message="Worse", pain_score=6, daily_function="WORSE"), previous
    )
    assert material and sensitive and changes["pain_delta"] == 3
    assert "self-report" in changes["attribution"]
    _, material, sensitive = compare_report(
        ClientReply(message="New treatment", treatment_changed=True), previous
    )
    assert material and not sensitive
    _, material, sensitive = compare_report(
        ClientReply(message="Something else happened", pain_score=3, daily_function="UNCHANGED"),
        previous,
    )
    assert material and sensitive  # Unclassified narrative changes require review.


@pytest.mark.integration
@integration[1]
def test_creation_is_scoped_idempotent_and_requires_timezone(setup):
    client, first, second = setup
    due = datetime.now(timezone.utc) + timedelta(days=7)
    created = create(client, first, scheduled_at=due)
    assert created.status_code == 201, created.text
    task = created.json()
    assert create(client, first, scheduled_at=due).json()["id"] == task["id"]
    assert create(client, first, scheduled_at=due, message="Different").status_code == 409
    assert (
        create(
            client, second, client_contact_id=first["assignment"]["patient_contact_id"]
        ).status_code
        == 422
    )
    assert create(client, first, key="naive", scheduled_at=datetime.now()).status_code == 422
    assert client.post(path(task) + "/client-replies", json={"message": "Hi"}).status_code in (
        401,
        403,
    )
    assert reply(client, task).status_code == 409
    assert (
        client.get(
            path(task).replace(first["firm_id"], second["firm_id"]), headers=AUTH
        ).status_code
        == 404
    )


@pytest.mark.integration
@integration[1]
async def test_future_task_waits_without_model_or_send_then_sends_once(setup, database_url):
    client, fixture, _ = setup
    due = datetime.now(timezone.utc) + timedelta(seconds=2)
    task = create(client, fixture, scheduled_at=due).json()
    db, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(task))
        assert outcome["status"] == "WAITING" and outcome["wake_at"] == due.isoformat()
        async with db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Execution)
                    .where(Execution.task_id == UUID(task["id"]))
                )
                == 0
            )
        await asyncio.sleep(max(0, (due - datetime.now(timezone.utc)).total_seconds()) + 0.1)
        sent = await worker.execute_task(activation(task, 1))
        assert sent["status"] == "WAITING" and sent["wait_for_event"]
        assert "wake_at" not in sent  # No model polling while waiting for an incoming reply.
        worker.settings.demo_max_activations = 1
        repeated = await worker.execute_task(activation(task, 2))
        assert repeated["status"] == "WAITING" and repeated["wait_for_event"]
        async with db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_client_check_in",
                    )
                )
                == 1
            )
    finally:
        await db.close()


@pytest.mark.integration
@integration[1]
async def test_reports_compare_with_same_client_and_material_update_reaches_supervisor(
    setup, database_url
):
    client, fixture, other = setup
    db, worker = runtime(database_url)
    supervisor = SupervisorActivities(db, worker.settings)
    try:
        first = create(client, fixture, key="first").json()
        await worker.execute_task(activation(first))
        baseline = reply(client, first)
        assert baseline.status_code == 201, baseline.text
        assert baseline.json()["report"]["meaningful"]
        assert not baseline.json()["report"]["needs_review"]
        assert reply(client, first).json()["replayed"]
        assert reply(client, first, message="Changed").status_code == 409
        assert (await worker.execute_task(activation(first, 1)))["status"] == "COMPLETED"
        coord_activation = {
            "tenant_id": fixture["firm_id"],
            "case_id": first["case_id"],
            "cycle": 0,
            "trigger": "event",
        }
        assert (await supervisor.coordinate_case(coord_activation))[
            "coordination_status"
        ] == "MONITORING"
        second = create(client, fixture, key="second").json()
        await worker.execute_task(activation(second))
        before = None
        async with db.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            before = coord.revision
        stable = reply(client, second)
        assert not stable.json()["report"]["meaningful"]
        assert (await worker.execute_task(activation(second, 1)))["status"] == "COMPLETED"
        async with db.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            assert coord.revision == before  # Routine unchanged reply and completion do not notify.
        third = create(client, fixture, key="third").json()
        await worker.execute_task(activation(third))
        changed = reply(
            client,
            third,
            pain_score=7,
            daily_function="WORSE",
            requests_help=True,
            message="I need a teammate's help.",
        )
        assert changed.status_code == 201, changed.text
        report = changed.json()["report"]
        assert report["previous_report_id"] == stable.json()["report"]["id"]
        assert report["changes"]["pain_delta"] == 4 and report["needs_review"]
        assert (await worker.execute_task(activation(third, 1)))["status"] == "BLOCKED"
        assert (await supervisor.coordinate_case({**coord_activation, "cycle": 1}))[
            "coordination_status"
        ] == "NEEDS_REVIEW"
        async with db.sessions() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            assert report["id"] in {u["report_id"] for u in coord.plan_json["client_updates"]}
            assert any("Client self-report" in r["question"] for r in coord.plan_json["reviews"])
        reports = client.get(
            f"/v1/firms/{fixture['firm_id']}/cases/{first['case_id']}/client-reports", headers=AUTH
        )
        assert len(reports.json()) == 3
        assert reports.json()[0]["attribution"] == "SIMULATED_CLIENT_SELF_REPORT"
        assert (
            client.get(
                f"/v1/firms/{other['firm_id']}/cases/{first['case_id']}/client-reports",
                headers=AUTH,
            ).status_code
            == 404
        )
    finally:
        await db.close()


@pytest.mark.integration
@integration[1]
async def test_completion_cannot_invent_reply_and_case_hold_blocks_check_in(setup, database_url):
    client, fixture, _ = setup
    task = create(client, fixture).json()
    db, worker = runtime(database_url)
    try:
        run = await worker._prepare(activation(task))
        decision = CheckInDecision(
            summary="Done",
            observations=[],
            report_id=uuid4(),
            next_action=NextAction(type="complete", follow_up_seconds=None, reason="Done"),
        )
        with pytest.raises(ValueError, match="not given"):
            await worker._commit(run, activation(task), decision, {})
        hold = client.post(
            f"/v1/firms/{task['tenant_id']}/cases/{task['case_id']}/restrictions",
            headers={**AUTH, "Idempotency-Key": "hold"},
            json={"expected_version": 0, "text": "Pause case contact"},
        )
        assert hold.status_code == 200
        assert (await worker._commit(run, activation(task), None, {}))["status"] == "CONTINUE"
        assert (await worker.execute_task(activation(task, 1)))["status"] == "BLOCKED"
        async with db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.task_id == UUID(task["id"]))
                )
                == 0
            )
    finally:
        await db.close()


@pytest.mark.integration
@integration[1]
async def test_schedule_and_reply_survive_temporal_worker_restart(setup, database_url):
    client, fixture, _ = setup
    task = create(
        client, fixture, scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=4)
    ).json()
    db, worker = runtime(database_url)
    temporal = await Client.connect("localhost:7233")
    queue = f"check-in-{uuid4()}"
    try:
        async with task_worker(db, worker.settings, temporal, queue):
            # Deliver both case and task outbox records, using the generic task worker only.
            for _ in range(5):
                if not await dispatch_once(db, temporal, queue, UUID(fixture["firm_id"])):
                    break
            await wait_for_status(db, task["id"], {"WAITING", "FAILED"})
        await asyncio.sleep(4)
        async with task_worker(db, worker.settings, temporal, queue):
            async with asyncio.timeout(20):
                while True:
                    response = await asyncio.to_thread(client.get, path(task), headers=AUTH)
                    if response.json()["client_check_in"]["sent_at"]:
                        break
                    await asyncio.sleep(0.1)
        reply_response = reply(client, task)
        assert reply_response.status_code == 201, reply_response.text
        async with task_worker(db, worker.settings, temporal, queue):
            for _ in range(5):
                if not await dispatch_once(db, temporal, queue, UUID(fixture["firm_id"])):
                    break
            assert await wait_for_status(db, task["id"], {"COMPLETED", "FAILED"}) == "COMPLETED"
        async with db.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ClientReport)
                    .where(ClientReport.tenant_id == UUID(fixture["firm_id"]))
                )
                == 1
            )
    finally:
        # This isolated test uses only a task worker; stop its deliberately unserved case workflow.
        await temporal.get_workflow_handle(
            f"firm/{fixture['firm_id']}/case/{task['case_id']}/supervisor"
        ).terminate()
        await db.close()


@pytest.mark.integration
@integration[1]
async def test_current_reply_in_saved_context_is_valid_without_redundant_tool_read(
    setup, database_url
):
    client, fixture, _ = setup
    task = create(client, fixture).json()
    db, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(task))
        result = reply(client, task)
        report = result.json()["report"]
        run = await worker._prepare(activation(task, 1))
        assert run.context["domain"]["state"]["check_in"]["report"]["id"] == report["id"]
        decision = CheckInDecision(
            summary="Client reports no material worsening",
            observations=[],
            report_id=report["id"],
            next_action=NextAction(
                type="complete", follow_up_seconds=None, reason="Saved reply available"
            ),
        )
        assert (await worker._commit(run, activation(task, 1), decision, {}))[
            "status"
        ] == "COMPLETED"
    finally:
        await db.close()


@pytest.mark.integration
@integration[1]
async def test_sdk_wrapped_stale_tool_error_refreshes_instead_of_failing(
    setup, database_url, monkeypatch
):
    from agents import Runner
    from agents.exceptions import UserError

    from agents_platform.execution import StaleExecution

    client, fixture, _ = setup
    task = create(client, fixture).json()
    db, worker = runtime(database_url)

    async def stale(*args, **kwargs):
        try:
            raise StaleExecution()
        except StaleExecution as exc:
            raise UserError("Wrapped tool error") from exc

    monkeypatch.setattr(Runner, "run", stale)
    try:
        assert (await worker.execute_task(activation(task)))["status"] == "CONTINUE"
        async with db.sessions() as session:
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]))
            )
            assert run.status == "STALE" and run.error_type is None
    finally:
        await db.close()

"""Calendar orchestration and client contact suppression, including manual tasks."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_check_in import activation as task_activation
from test_check_in import create as create_checkin
from test_check_in import reply
from test_check_in_series import create_series
from test_supervisor import activation as supervisor_activation
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.execution import TaskActivities
from agents_platform.models import (
    ClientCheckIn,
    ClientReport,
    ToolInvocation,
)
from agents_platform.monthly_check_ins import MonthlyActivities

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


@pytest.fixture
def clock(monkeypatch):
    import agents_platform.check_in_api as intake
    import agents_platform.check_in_contact as contact
    import agents_platform.check_in_series as series_module
    import agents_platform.client_check_in as adapter
    import agents_platform.execution as execution
    import agents_platform.monthly_check_ins as monthly

    class Clock:
        at = datetime(2027, 1, 1, tzinfo=timezone.utc)

        def now(self):
            return self.at

    value = Clock()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.at.astimezone(tz) if tz else value.at.replace(tzinfo=None)

    monkeypatch.setattr(monthly, "now", value.now)
    monkeypatch.setattr(contact, "now", value.now)
    monkeypatch.setattr(series_module, "now", value.now)
    for module in (adapter, intake, execution):
        monkeypatch.setattr(module, "datetime", FrozenDatetime)
    return value


@pytest.fixture
async def services(database_url):
    database = Database(database_url)
    settings = Settings(_env_file=None, agent_mode="scripted", demo_follow_up_seconds=60)
    yield (
        database,
        MonthlyActivities(database),
        SupervisorActivities(database, settings),
        TaskActivities(database, settings),
    )
    await database.close()


def root(fixture):
    return (
        f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/monthly-check-ins"
    )


def create(client, fixture, key="monthly", **changes):
    return client.post(
        root(fixture),
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "client_contact_id": fixture["assignment"]["patient_contact_id"],
            "timezone": "UTC",
            "day_of_month": 5,
            "local_time": "09:00",
            "max_occurrences": 3,
            **changes,
        },
    )


def current(client, fixture, row):
    return client.get(root(fixture) + "/" + row["id"], headers=AUTH).json()


def activation(fixture, row):
    return {
        "tenant_id": fixture["firm_id"],
        "case_id": fixture["assignment"]["case_id"],
        "schedule_id": row["id"],
    }


def control(client, fixture, row, action, key=None):
    row = current(client, fixture, row)
    return client.post(
        root(fixture) + f"/{row['id']}/control",
        headers={**AUTH, "Idempotency-Key": key or action},
        json={
            "expected_version": row["version"],
            "action": action,
            "reason": "Operator requested " + action,
        },
    )


async def tick(client, fixture, row, services, clock, *, month=1, cycle=0):
    clock.at = datetime(2027, month, 5, 9, 0, 1, tzinfo=timezone.utc)
    await services[1].advance_monthly(activation(fixture, row))
    await services[2].coordinate_case(supervisor_activation(fixture, cycle))
    return current(client, fixture, row)


async def completed_manual(client, fixture, worker, clock):
    response = create_checkin(client, fixture, scheduled_at=clock.at)
    assert response.status_code == 201, response.text
    task = response.json()
    assert (await worker.execute_task(task_activation(task)))["status"] == "WAITING"
    assert reply(client, task, pain_score=2, message="Doing okay").status_code == 201
    assert (await worker.execute_task(task_activation(task, 1)))["status"] == "COMPLETED"
    return task


def test_monthly_api_scope_replay_and_cadence_exclusion(setup, clock):
    client, fixture, other = setup
    assert client.post(root(fixture), json={}).status_code in (401, 403)
    response = create(client, fixture)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["minimum_gap_days"] == 28 and row["next_due_at"].startswith("2027-01-05T09:00")
    assert create(client, fixture).json()["replayed"]
    assert create(client, fixture, day_of_month=6).status_code == 409
    assert create(client, fixture, key="duplicate").status_code == 409
    assert create_series(client, fixture).status_code == 409
    assert (
        create(
            client, other, client_contact_id=fixture["assignment"]["patient_contact_id"]
        ).status_code
        == 422
    )
    assert client.get(root(other) + "/" + row["id"], headers=AUTH).status_code == 404
    group = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{row['group_task_id']}", headers=AUTH
    ).json()
    assert group["monthly_check_in_schedule"]["id"] == row["id"]


async def test_timer_only_wakes_supervisor_then_supervisor_delegates_once(setup, services, clock):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    clock.at = datetime(2027, 1, 5, 9, 0, 1, tzinfo=timezone.utc)
    await asyncio.gather(*(services[1].advance_monthly(activation(fixture, row)) for _ in range(3)))
    due = current(client, fixture, row)
    assert len(due["occurrences"]) == 1 and due["occurrences"][0]["status"] == "DUE"
    assert due["occurrences"][0]["task_id"] is None
    await asyncio.gather(
        *(services[2].coordinate_case(supervisor_activation(fixture)) for _ in range(2))
    )
    delegated = current(client, fixture, row)["occurrences"][0]
    assert delegated["status"] == "DELEGATED" and delegated["decision_source_id"]
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{delegated['task_id']}", headers=AUTH
    ).json()
    assert task["agent_key"] == "client_check_in"
    assert (await services[3].execute_task(task_activation(task)))["status"] == "WAITING"
    async with services[0].sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ClientCheckIn)
                .where(ClientCheckIn.monthly_occurrence_id == UUID(delegated["id"]))
            )
            == 1
        )


async def test_manual_checkin_two_days_before_monthly_suppresses_that_month(setup, services, clock):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    clock.at = datetime(2027, 1, 3, 10, tzinfo=timezone.utc)
    manual = await completed_manual(client, fixture, services[3], clock)
    result = await tick(client, fixture, row, services, clock)
    occurrence = result["occurrences"][0]
    assert occurrence["status"] == "SKIPPED" and occurrence["task_id"] is None
    assert occurrence["covering_task_id"] == manual["id"]
    assert "recent" in occurrence["reason"]
    assert result["next_due_at"].startswith("2027-02-05")
    # Next calendar month remains eligible; skipping does not move the calendar by 28 days.
    result = await tick(client, fixture, row, services, clock, month=2, cycle=1)
    assert result["occurrences"][0]["status"] == "DELEGATED"


async def test_recent_client_reply_also_suppresses_even_if_send_was_old(setup, services, clock):
    client, fixture, _ = setup
    clock.at = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
    manual = await completed_manual(client, fixture, services[3], clock)
    async with services[0].sessions.begin() as session:
        report = await session.scalar(
            select(ClientReport).where(
                ClientReport.client_contact_id == UUID(fixture["assignment"]["patient_contact_id"])
            )
        )
        report.reported_at = datetime(2027, 1, 4, 9, tzinfo=timezone.utc)
    clock.at = datetime(2027, 1, 1, tzinfo=timezone.utc)
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    assert result["occurrences"][0]["status"] == "SKIPPED"
    assert "client reply" in result["occurrences"][0]["reason"]
    assert result["occurrences"][0]["covering_task_id"] == manual["id"]


async def test_pending_manual_task_is_reused_instead_of_another_monthly_message(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    manual = create_checkin(
        client, fixture, scheduled_at=datetime(2027, 1, 5, 9, tzinfo=timezone.utc)
    ).json()
    result = await tick(client, fixture, row, services, clock)
    assert result["occurrences"][0]["status"] == "SKIPPED"
    assert result["occurrences"][0]["covering_task_id"] == manual["id"]
    assert (await services[3].execute_task(task_activation(manual)))["status"] == "WAITING"


async def test_unanswered_prior_month_blocks_another_send_after_spacing_passes(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    await services[3].execute_task(task_activation(task))
    result = await tick(client, fixture, row, services, clock, month=2, cycle=1)
    assert result["occurrences"][0]["status"] == "SKIPPED"
    assert "awaiting reply" in result["occurrences"][0]["reason"]


async def test_pause_manual_contact_then_resume_suppresses_precreated_monthly_child(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    monthly_task_id = result["occurrences"][0]["task_id"]
    assert control(client, fixture, row, "PAUSE").status_code == 200
    manual = await completed_manual(client, fixture, services[3], clock)
    assert control(client, fixture, row, "RESUME").status_code == 200
    await services[2].coordinate_case(supervisor_activation(fixture, 1))
    result = current(client, fixture, row)
    assert result["occurrences"][0]["status"] == "SKIPPED"
    assert result["occurrences"][0]["covering_task_id"] == manual["id"]
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{monthly_task_id}", headers=AUTH
    ).json()
    assert task["status"] == "CANCELLED"
    async with services[0].sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id == UUID(monthly_task_id),
                    ToolInvocation.tool_name == "send_client_check_in",
                )
            )
            == 0
        )


async def test_holds_and_open_reviews_block_without_becoming_permission(setup, services, clock):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    url = f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}"
    hold = client.post(
        url + "/restrictions",
        headers={**AUTH, "Idempotency-Key": "hold"},
        json={"expected_version": 0, "text": "Do not contact this client."},
    ).json()
    result = await tick(client, fixture, row, services, clock)
    assert result["occurrences"][0]["status"] == "BLOCKED"
    version = client.get(url + "/memory", headers=AUTH).json()["case_memory_version"]
    assert (
        client.post(
            url + "/restrictions",
            headers={**AUTH, "Idempotency-Key": "resolve"},
            json={
                "expected_version": version,
                "operation": "RESOLVE",
                "item_id": hold["item_id"],
                "text": "Explicit operator release.",
            },
        ).status_code
        == 200
    )
    await services[2].coordinate_case(supervisor_activation(fixture, 1))
    assert current(client, fixture, row)["occurrences"][0]["status"] == "DELEGATED"


async def test_missed_months_do_not_catch_up_and_stop_cancels_unsent(setup, services, clock):
    client, fixture, _ = setup
    row = create(client, fixture, max_occurrences=6).json()
    clock.at = datetime(2027, 3, 10, tzinfo=timezone.utc)
    await services[1].advance_monthly(activation(fixture, row))
    result = current(client, fixture, row)
    assert len(result["occurrences"]) == 3
    assert {o["status"] for o in result["occurrences"]} == {"MISSED"}
    assert result["next_due_at"].startswith("2027-04-05")
    result = await tick(client, fixture, row, services, clock, month=4)
    child = result["occurrences"][0]["task_id"]
    assert control(client, fixture, row, "STOP").status_code == 200
    assert (
        client.get(f"/v1/firms/{fixture['firm_id']}/tasks/{child}", headers=AUTH).json()["status"]
        == "CANCELLED"
    )
    assert control(client, fixture, row, "RESUME").status_code == 409


async def test_end_window_no_catchup_and_contact_spacing_applies_to_manual_tasks(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture, max_occurrences=1).json()
    result = await tick(client, fixture, row, services, clock)
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    clock.at = datetime(2027, 1, 6, 10, tzinfo=timezone.utc)
    await services[1].advance_monthly(activation(fixture, row))
    assert current(client, fixture, row)["occurrences"][0]["status"] == "EXPIRED"
    assert (await services[3].execute_task(task_activation(task)))["status"] == "CANCELLED"
    clock.at = datetime(2027, 1, 7, 10, tzinfo=timezone.utc)
    manual = await completed_manual(client, fixture, services[3], clock)
    clock.at += timedelta(days=2)
    denied = create_checkin(client, fixture, key="too-soon", scheduled_at=clock.at)
    assert denied.status_code == 409 and "28" in denied.text
    assert manual["id"]


async def test_send_time_gate_rechecks_contact_even_without_a_task_version_change(
    setup, services, clock
):
    from agents.tool_context import ToolContext

    client, fixture, _ = setup
    clock.at = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
    manual = await completed_manual(client, fixture, services[3], clock)
    clock.at = datetime(2027, 1, 1, tzinfo=timezone.utc)
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    prepared = await services[3]._prepare(task_activation(task))
    # Simulate another intake path recording/correcting a recent actual contact after preparation.
    async with services[0].sessions.begin() as session:
        earlier = await session.scalar(
            select(ClientCheckIn).where(ClientCheckIn.task_id == UUID(manual["id"]))
        )
        earlier.sent_at = clock.at - timedelta(days=2)
    send = next(t for t in services[3]._tools(prepared) if t.name == "send_client_check_in")
    with pytest.raises(ValueError, match="recent"):
        await send.on_invoke_tool(
            ToolContext(
                context=None, tool_name=send.name, tool_call_id="late-contact", tool_arguments="{}"
            ),
            "{}",
        )
    async with services[0].sessions() as session:
        saved = await session.scalar(
            select(ClientCheckIn).where(ClientCheckIn.task_id == UUID(task["id"]))
        )
        assert saved.sent_at is None
    await services[2].coordinate_case(supervisor_activation(fixture, 1))
    assert current(client, fixture, row)["occurrences"][0]["status"] == "SKIPPED"


async def test_open_sensitive_report_blocks_next_month_even_after_gap(setup, services, clock):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    await services[3].execute_task(task_activation(task))
    assert reply(client, task, pain_score=9, requests_help=True).status_code == 201
    assert (await services[3].execute_task(task_activation(task, 1)))["status"] == "BLOCKED"
    result = await tick(client, fixture, row, services, clock, month=2, cycle=1)
    assert result["occurrences"][0]["status"] == "BLOCKED"
    assert "human review" in result["occurrences"][0]["reason"]
    assert result["occurrences"][0]["task_id"] is None


async def test_provider_only_hold_and_another_firms_contact_do_not_suppress_client(
    setup, services, clock
):
    client, fixture, other = setup
    clock.at = datetime(2027, 1, 3, 10, tzinfo=timezone.utc)
    await completed_manual(client, other, services[3], clock)
    clock.at = datetime(2027, 1, 1, tzinfo=timezone.utc)
    row = create(client, fixture).json()
    url = f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/restrictions"
    assert (
        client.post(
            url,
            headers={**AUTH, "Idempotency-Key": "provider-only"},
            json={
                "expected_version": 0,
                "provider_contact_id": fixture["assignment"]["provider_contact_id"],
                "text": "Do not contact this hospital.",
            },
        ).status_code
        == 200
    )
    result = await tick(client, fixture, row, services, clock)
    assert result["occurrences"][0]["status"] == "DELEGATED"


async def test_case_closure_cancels_unsent_monthly_work(setup, services, clock):
    from agents_platform.models import Case

    client, fixture, _ = setup
    row = create(client, fixture).json()
    result = await tick(client, fixture, row, services, clock)
    async with services[0].sessions.begin() as session:
        case = await session.get(Case, UUID(fixture["assignment"]["case_id"]))
        case.status = "CLOSED"
    assert (await services[1].advance_monthly(activation(fixture, row)))["status"] == "CLOSED"
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    assert task["status"] == "CANCELLED"


async def test_seven_calendar_months_keep_one_task_per_slot_and_finish_at_bound(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture, max_occurrences=7).json()
    task_ids = []
    for month in range(1, 8):
        result = await tick(client, fixture, row, services, clock, month=month, cycle=month * 2)
        occurrence = result["occurrences"][0]
        assert occurrence["month"] == f"2027-{month:02d}"
        assert occurrence["status"] == "DELEGATED"
        task = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{occurrence['task_id']}", headers=AUTH
        ).json()
        task_ids.append(task["id"])
        assert (await services[3].execute_task(task_activation(task)))["status"] == "WAITING"
        assert reply(client, task, pain_score=2, message="Routine monthly reply").status_code == 201
        assert (await services[3].execute_task(task_activation(task, 1)))["status"] == "COMPLETED"
        await services[1].advance_monthly(activation(fixture, row))
        assert len(current(client, fixture, row)["occurrences"]) == month
    assert len(set(task_ids)) == 7
    assert current(client, fixture, row)["status"] == "FINISHED"
    async with services[0].sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id.in_([UUID(i) for i in task_ids]),
                    ToolInvocation.tool_name == "send_client_check_in",
                )
            )
            == 7
        )


async def test_calendar_timer_and_supervisor_handoff_survive_worker_restart(setup, database_url):
    from contextlib import suppress
    from uuid import uuid4

    from temporalio.api.enums.v1 import EventType
    from temporalio.client import Client
    from temporalio.worker import Worker

    from agents_platform.dispatch import dispatch_once
    from agents_platform.models import MonthlyCheckInSchedule
    from agents_platform.orchestration.workflows import (
        CaseWorkflow,
        MonthlyCheckInWorkflow,
        TaskWorkflow,
    )

    client, fixture, _ = setup
    row = create(client, fixture, max_occurrences=1).json()
    database = Database(database_url)
    settings = Settings(_env_file=None, agent_mode="scripted", demo_follow_up_seconds=60)
    specialist = TaskActivities(database, settings)
    supervisor = SupervisorActivities(database, settings)
    scheduler = MonthlyActivities(database)
    temporal = await Client.connect("localhost:7233")
    queue = f"monthly-{uuid4()}"
    due_at = datetime.now(timezone.utc) + timedelta(seconds=8)
    async with database.sessions.begin() as session:
        saved = await session.get(MonthlyCheckInSchedule, UUID(row["id"]))
        saved.next_due_at = due_at

    def worker():
        return Worker(
            temporal,
            task_queue=queue,
            workflows=[MonthlyCheckInWorkflow, CaseWorkflow, TaskWorkflow],
            activities=[
                scheduler.advance_monthly,
                supervisor.coordinate_case,
                supervisor.fail_supervisor,
                specialist.execute_task,
                specialist.fail_task,
            ],
        )

    async def pump(predicate):
        async with asyncio.timeout(35):
            while True:
                await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
                if await predicate():
                    return
                await asyncio.sleep(0.1)

    async def timer_started():
        try:
            history = await temporal.get_workflow_handle(row["workflow_id"]).fetch_history()
            return any(e.event_type == EventType.EVENT_TYPE_TIMER_STARTED for e in history.events)
        except Exception:
            return False

    async def sent():
        async with database.sessions() as session:
            return (
                await session.scalar(
                    select(ClientCheckIn.sent_at).where(
                        ClientCheckIn.tenant_id == UUID(fixture["firm_id"]),
                        ClientCheckIn.monthly_occurrence_id.is_not(None),
                    )
                )
                is not None
            )

    try:
        async with worker():
            await pump(timer_started)
        await asyncio.sleep(max(0, (due_at - datetime.now(timezone.utc)).total_seconds()) + 0.2)
        async with worker():
            await pump(sent)
        # Re-deliver a wake to an existing calendar workflow with the worker offline.
        await temporal.get_workflow_handle(row["workflow_id"]).signal("wake")
        async with worker():
            await pump(sent)
        async with database.sessions() as session:
            checkins = (
                await session.scalars(
                    select(ClientCheckIn).where(
                        ClientCheckIn.tenant_id == UUID(fixture["firm_id"]),
                        ClientCheckIn.monthly_occurrence_id.is_not(None),
                    )
                )
            ).all()
            assert len(checkins) == 1
            task_id = checkins[0].task_id
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == task_id,
                        ToolInvocation.tool_name == "send_client_check_in",
                    )
                )
                == 1
            )
    finally:
        async with database.sessions() as session:
            task_ids = (
                await session.scalars(
                    select(ClientCheckIn.task_id).where(
                        ClientCheckIn.tenant_id == UUID(fixture["firm_id"]),
                        ClientCheckIn.monthly_occurrence_id.is_not(None),
                    )
                )
            ).all()
        ids = [
            row["workflow_id"],
            f"firm/{fixture['firm_id']}/case/{fixture['assignment']['case_id']}/supervisor",
            *[f"firm/{fixture['firm_id']}/task/{i}" for i in task_ids],
        ]
        for workflow_id in ids:
            with suppress(Exception):
                await temporal.get_workflow_handle(workflow_id).terminate()
        await database.close()


async def test_distant_future_manual_assignment_does_not_suppress_an_earlier_month(
    setup, services, clock
):
    client, fixture, _ = setup
    row = create(client, fixture).json()
    planned = create_checkin(
        client,
        fixture,
        key="future-manual",
        scheduled_at=datetime(2027, 2, 10, 9, tzinfo=timezone.utc),
    )
    assert planned.status_code == 201, planned.text
    result = await tick(client, fixture, row, services, clock)
    assert result["occurrences"][0]["status"] == "DELEGATED"
    task = client.get(
        f"/v1/firms/{fixture['firm_id']}/tasks/{result['occurrences'][0]['task_id']}", headers=AUTH
    ).json()
    assert (await services[3].execute_task(task_activation(task)))["status"] == "WAITING"


async def test_replacement_interval_series_waits_for_existing_contact_spacing(
    setup, services, clock
):
    from agents_platform.check_in_series import SeriesActivities
    from agents_platform.models import CheckInSeries

    client, fixture, _ = setup
    row = create(client, fixture).json()
    assert control(client, fixture, row, "STOP").status_code == 200
    clock.at = datetime(2027, 1, 3, 10, tzinfo=timezone.utc)
    await completed_manual(client, fixture, services[3], clock)
    clock.at = datetime(2027, 1, 5, 9, tzinfo=timezone.utc)
    response = create_series(
        client, fixture, interval_seconds=28 * 86400, first_due_at=clock.at.isoformat()
    )
    assert response.status_code == 201, response.text
    series = response.json()
    outcome = await SeriesActivities(services[0]).advance_series(
        {
            "tenant_id": fixture["firm_id"],
            "case_id": fixture["assignment"]["case_id"],
            "series_id": series["id"],
        }
    )
    assert outcome["wake_at"].startswith("2027-01-31")
    async with services[0].sessions() as session:
        saved = await session.get(CheckInSeries, UUID(series["id"]))
        assert saved.status == "ACTIVE" and saved.active_task_id is None

"""Regression from the browser rehearsal: a specific review replaces generic outreach."""

import os
from uuid import UUID

import pytest
from sqlalchemy import select
from test_memory import activation, runtime
from test_provider_response_scenario import ingest
from test_task_persistence import database_url as database_url
from test_task_persistence import post
from test_task_persistence import setup as setup

from agents_platform.human_request_handlers import backfill_blocked_requests, ensure_records_request
from agents_platform.models import HumanRequest, Task, TaskMemoryItem

pytestmark = pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="PostgreSQL")


async def test_specific_authorization_review_retires_generic_follow_up_without_clearing_hold(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture, body={**fixture["assignment"], "agent_version": 4}).json()
    database, worker = runtime(database_url)
    try:
        async with database.sessions.begin() as session:
            saved = await session.get(Task, UUID(task["id"]))
            generic = await ensure_records_request(session, saved, "Inspect provider updates")
            generic_id = generic.id
        received = ingest(client, task)
        assert received.status_code == 202, received.text
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "BLOCKED"
        await backfill_blocked_requests(database)
        await backfill_blocked_requests(
            database
        )  # Worker restart must not recreate the generic form.
        async with database.sessions() as session:
            generic = await session.get(HumanRequest, generic_id)
            assert generic.status == "SUPERSEDED"
            rows = (
                await session.scalars(
                    select(HumanRequest).where(
                        HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                    )
                )
            ).all()
            assert len(rows) == 1 and rows[0].handler == "provider_response.v1"
            hold = await session.scalar(
                select(TaskMemoryItem).where(
                    TaskMemoryItem.task_id == UUID(task["id"]),
                    TaskMemoryItem.kind == "OBLIGATION",
                    TaskMemoryItem.status == "OPEN",
                )
            )
            assert hold is not None
    finally:
        await database.close()


async def test_invalid_supervisor_citation_replans_then_offers_technical_retry_only(
    setup, database_url, monkeypatch
):
    from uuid import uuid4

    from test_supervisor import activation as case_activation
    from test_supervisor import runtime as supervisor_runtime
    from test_supervisor import submit
    from test_task_persistence import AUTH

    from agents_platform.coordination import SupervisorAction, SupervisorDecision

    client, fixture, _ = setup
    created = submit(client, fixture).json()
    database, supervisor = supervisor_runtime(database_url)
    original = supervisor._decide

    async def bad(*args):
        return SupervisorDecision(
            summary="Try the approved assignment",
            evidence_event_ids=[uuid4()],
            actions=[SupervisorAction(type="create_task", target_index=0, question=None)],
            objective_satisfied=False,
            next_review_at=None,
        ), {}

    monkeypatch.setattr(supervisor, "_decide", bad)
    try:
        for cycle in range(2):
            assert (await supervisor.coordinate_case(case_activation(fixture, cycle)))[
                "status"
            ] == "CONTINUE"
        final = await supervisor.coordinate_case(case_activation(fixture, 2))
        assert final["coordination_status"] == "NEEDS_REVIEW"
        rows = client.get(
            f"/v1/firms/{fixture['firm_id']}/human-requests",
            headers=AUTH,
            params={"task_id": created["group_task_id"], "status": "OPEN"},
        ).json()["items"]
        assert len(rows) == 1 and rows[0]["handler"] == "supervisor_recovery.v1"
        row = rows[0]
        assert [a["action"] for a in row["allowed_actions"]] == ["RETRY_SUPERVISOR"]
        async with database.sessions() as session:
            assert not await session.scalar(
                select(Task.id).where(
                    Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT"
                )
            )
        response = client.post(
            f"/v1/firms/{fixture['firm_id']}/human-requests/{row['id']}/decisions",
            headers={**AUTH, "Idempotency-Key": "retry-supervisor"},
            json={
                "expected_task_version": row["task_version"],
                "expected_request_version": row["version"],
                "action": "RETRY_SUPERVISOR",
                "reason": "Technical issue reviewed; retry unchanged scope.",
                "payload": {},
            },
        )
        assert response.status_code == 200, response.text
        monkeypatch.setattr(supervisor, "_decide", original)
        await supervisor.coordinate_case(case_activation(fixture, 3))
        async with database.sessions() as session:
            tasks = (
                await session.scalars(
                    select(Task).where(
                        Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT"
                    )
                )
            ).all()
            assert len(tasks) == 1
    finally:
        await database.close()


async def test_fresh_business_event_is_processed_after_many_supervisor_decisions(
    setup, database_url
):
    from test_supervisor import activation as case_activation
    from test_supervisor import runtime as supervisor_runtime
    from test_supervisor import submit

    from agents_platform.models import CaseCoordination

    client, fixture, _ = setup
    submit(client, fixture)
    database, supervisor = supervisor_runtime(database_url)
    try:
        async with database.sessions.begin() as session:
            coord = await session.scalar(
                select(CaseCoordination).where(
                    CaseCoordination.tenant_id == UUID(fixture["firm_id"])
                )
            )
            coord.plan_json = {**coord.plan_json, "decision_count": 300, "automatic_decisions": 30}
        await supervisor.coordinate_case(case_activation(fixture))
        async with database.sessions() as session:
            assert await session.scalar(
                select(Task.id).where(
                    Task.tenant_id == UUID(fixture["firm_id"]), Task.kind == "AGENT"
                )
            )
    finally:
        await database.close()


async def test_parent_and_specialist_activity_stay_separate(setup, database_url):
    from test_task_persistence import AUTH

    client, fixture, other = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        url = f"/v1/operator/firms/{fixture['firm_id']}/activity"
        group = fixture["assignment"]["parent_task_id"]
        feed = client.get(url, headers=AUTH, params={"task_id": group}).json()["items"]
        updates = [
            e
            for e in feed
            if e["task_id"] == task["id"]
            and e["actor"].startswith("agent/medical_records_collector/")
        ]
        assert not updates
        assert all(e["task_id"] == group for e in feed)
        child = client.get(url, headers=AUTH, params={"task_id": task["id"]}).json()["items"]
        assert any(
            e["actor"].startswith("agent/medical_records_collector/")
            and e["payload"].get("summary")
            for e in child
        )
        assert (
            client.get(
                url, headers=AUTH, params={"task_id": other["assignment"]["parent_task_id"]}
            ).status_code
            == 404
        )
    finally:
        await database.close()


async def test_task_list_exposes_parent_subtask_links_and_filters_parent_status(
    setup, database_url
):
    from uuid import uuid4

    from test_task_persistence import AUTH

    from agents_platform.models import CaseCoordination

    client, f, other = setup
    task = post(client, f).json()
    database, worker = runtime(database_url)
    group = f["assignment"]["parent_task_id"]
    try:
        await worker.execute_task(activation(f, task))
        async with database.sessions.begin() as session:
            session.add(
                CaseCoordination(
                    id=uuid4(),
                    tenant_id=UUID(f["firm_id"]),
                    case_id=UUID(task["case_id"]),
                    group_task_id=UUID(group),
                    workflow_id=f"hierarchy-test/{uuid4()}",
                    agent_version=5,
                    status="NEEDS_REVIEW",
                    objective_json={"objective": "Collect records"},
                )
            )
        url = f"/v1/operator/firms/{f['firm_id']}/tasks"
        page = client.get(url, headers=AUTH, params={"include_parents": True}).json()
        assert {r["id"] for r in page["items"]} == {group, task["id"]}
        parent = next(r for r in page["items"] if r["id"] == group)
        assert parent["status"] == "BLOCKED" and parent["kind"] == "GROUP"
        assert parent["subtasks"][0]["id"] == task["id"]
        filtered = client.get(
            url, headers=AUTH, params={"include_parents": True, "status": "BLOCKED"}
        ).json()
        assert [r["id"] for r in filtered["items"]] == [group]
        first = client.get(url, headers=AUTH, params={"include_parents": True, "limit": 1}).json()
        second = client.get(
            url,
            headers=AUTH,
            params={"include_parents": True, "limit": 1, "before": first["next_cursor"]},
        ).json()
        assert {first["items"][0]["id"], second["items"][0]["id"]} == {group, task["id"]}
        assert not second["next_cursor"]
        assert len(client.get(url, headers=AUTH).json()["items"]) == 1
        assert (
            client.get(
                url,
                headers=AUTH,
                params={"include_parents": True, "case_id": other["assignment"]["case_id"]},
            ).json()["items"]
            == []
        )
    finally:
        await database.close()


async def test_new_records_tasks_allow_time_to_supply_a_provider_update(setup, database_url):
    from agents_platform.models import RecordsRequest

    client, f, _ = setup
    task = post(client, f).json()
    database, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(f, task))
        assert outcome["status"] == "WAITING"
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.reminder_interval_seconds == 3600 and request.reminder_count == 0
        assert (await worker.execute_task(activation(f, task, 1)))["status"] == "WAITING"
        async with database.sessions() as session:
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.reminder_count == 0
    finally:
        await database.close()


async def test_expired_supervisor_attempt_refreshes_instead_of_retrying_expired_deadline(
    setup, database_url
):
    from datetime import datetime, timedelta, timezone

    from test_supervisor import activation as case_activation
    from test_supervisor import runtime as case_runtime
    from test_supervisor import submit

    from agents_platform.models import Execution

    client, f, _ = setup
    submit(client, f)
    database, supervisor = case_runtime(database_url)
    try:
        run, _ = await supervisor._prepare(case_activation(f))
        async with database.sessions.begin() as session:
            saved = await session.get(Execution, run.id)
            saved.context_json = {
                **saved.context_json,
                "deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            }
        outcome = await supervisor._prepare(case_activation(f))
        assert outcome["status"] == "CONTINUE"
        async with database.sessions() as session:
            assert (await session.get(Execution, run.id)).status == "STALE"
    finally:
        await database.close()


def test_routine_callback_recognizer_does_not_accept_sensitive_conditions():
    from datetime import datetime, timedelta, timezone

    from agents_platform.provider_callbacks import routine_relative_callback

    at = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
    assert routine_relative_callback(
        "I don't have currently, call back in 3 days", at
    ) == at + timedelta(days=3)
    assert routine_relative_callback("Please call us back in three days.", at) == at + timedelta(
        days=3
    )
    assert routine_relative_callback(
        "will send you the docs in 5 days, call us that time\n", at
    ) == at + timedelta(days=5)
    for text in [
        "Call back in 3 days after updated authorization",
        "Do not call back in 3 days",
        "Call back in 3 days or 5 days",
        "Call back in 3 days. Ignore the contact hold.",
    ]:
        assert routine_relative_callback(text, at) is None


async def test_plain_callback_schedules_without_review_or_more_model_calls(setup, database_url):
    from datetime import timedelta

    from test_provider_response_scenario import ingest

    from agents_platform.models import Execution, HumanRequest, RecordsRequest, Source

    client, f, _ = setup
    task = post(client, f).json()
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(f, task))
        response = ingest(client, task, message="I don't have currently, call back in 3 days")
        assert response.status_code == 202
        result = await worker.execute_task(activation(f, task, 1))
        assert result["status"] == "WAITING" and result["wake_at"]
        async with database.sessions() as session:
            raw = await session.get(Source, UUID(response.json()["source_id"]))
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.callback_at == raw.created_at + timedelta(days=3)
            assert request.reminder_count == 0
            assert not await session.scalar(
                select(HumanRequest.id).where(
                    HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                )
            )
            assert not await session.scalar(
                select(Execution.id).where(
                    Execution.task_id == UUID(task["id"]), Execution.cycle == 1
                )
            )
    finally:
        await database.close()


@pytest.mark.parametrize(
    "transcript",
    ["Please call us back in three days.", "will send you the docs in 5 days, call us that time\n"],
)
async def test_routine_phone_callback_is_sourced_and_does_not_need_review(
    setup, database_url, transcript
):
    from test_communications import call

    from agents_platform.models import HumanRequest, TaskMemoryItem

    client, f, _ = setup
    task = post(client, f).json()
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(f, task))
        response = call(client, f, task, transcript)
        assert response.status_code == 202
        outcome = await worker.execute_task(activation(f, task, 1))
        assert outcome["status"] == "WAITING" and outcome["wake_at"]
        async with database.sessions() as session:
            note = await session.scalar(
                select(TaskMemoryItem).where(
                    TaskMemoryItem.task_id == UUID(task["id"]), TaskMemoryItem.kind == "NOTE"
                )
            )
            assert note and note.due_at and note.source_id
            assert not await session.scalar(
                select(HumanRequest.id).where(
                    HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                )
            )
    finally:
        await database.close()


async def test_technical_recovery_preserves_open_business_review(setup, database_url):
    from uuid import uuid4

    from test_recovery_contact import failed_after_send, resolve

    from agents_platform.human_requests import ensure_request

    client, f, _ = setup
    database, worker = runtime(database_url)
    try:
        task, incident = await failed_after_send(client, f, worker)
        async with database.sessions.begin() as session:
            saved = await session.get(Task, UUID(task["id"]))
            review = await ensure_request(
                session,
                saved,
                handler="test_business_review.v1",
                subject_id=uuid4(),
                subject_fingerprint="still-needs-authorization",
                question="Provide updated authorization.",
                context_summary="A technical retry cannot approve it.",
                actions=[],
            )
            review_id = review.id
        response = resolve(client, f, incident)
        assert response.status_code == 200, response.text
        async with database.sessions() as session:
            assert (await session.get(HumanRequest, review_id)).status == "OPEN"
    finally:
        await database.close()

"""Partial evidence narrows every subsequent follow-up and never completes early."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_memory import activation, change, runtime
from test_records_reminders import due
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.models import HumanContinuation, RecordEvidence, Source, ToolInvocation
from agents_platform.task_contracts import NextAction, RecordsDecision

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def create(client, fixture):
    body = {
        **fixture["assignment"],
        "agent_version": 3,
        "requirements": [
            {"record_type": kind, "all_dates": True} for kind in ("RECORDS", "BILLS", "IMAGING")
        ],
    }
    result = post(client, fixture, body=body)
    assert result.status_code == 201, result.text
    return result.json()


def path(task):
    return f"/v1/firms/{task['tenant_id']}/tasks/{task['id']}"


def receive(client, task, kinds=(), status="PARTIAL", key="delivery", **extra):
    ids = [r["id"] for r in task["records_request"]["requirements"] if r["record_type"] in kinds]
    return client.post(
        path(task) + "/provider-updates",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "status": status,
            "message": "Selected synthetic items delivered.",
            **({"received_requirement_ids": ids} if kinds else {}),
            **extra,
        },
    )


def current(client, task):
    return client.get(path(task), headers=AUTH).json()


async def test_bills_then_records_then_imaging_narrow_reminders_and_complete(setup, database_url):
    client, fixture, _ = setup
    task = create(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        bills = receive(client, task, ["BILLS"])
        assert bills.status_code == 201, bills.text
        assert receive(client, task, ["BILLS"]).json()["replayed"]
        partial = current(client, task)
        assert partial["records_request"]["status"] == "PARTIAL"
        states = {r["record_type"]: r["status"] for r in partial["records_request"]["requirements"]}
        assert states == {"BILLS": "RECEIVED", "RECORDS": "OUTSTANDING", "IMAGING": "OUTSTANDING"}
        prepared = await worker._prepare(activation(fixture, task, 1))
        assert isinstance(prepared, dict) and prepared["status"] == "WAITING"
        await due(database, task)
        result = await worker.execute_task(activation(fixture, task, 2))
        assert result["status"] == "WAITING"
        async with database.sessions() as session:
            reminder = await session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_reminder",
                )
            )
            assert {i["record_type"] for i in reminder.result_json["requested_items"]} == {
                "RECORDS",
                "IMAGING",
            }
            assert "bills" not in reminder.result_json["message"]
        assert receive(client, task, ["RECORDS"], key="records").status_code == 201
        await due(database, task)
        assert (await worker.execute_task(activation(fixture, task, 3)))["status"] == "WAITING"
        async with database.sessions() as session:
            reminder = await session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.logical_key == "records-reminder/2",
                )
            )
            assert [i["record_type"] for i in reminder.result_json["requested_items"]] == [
                "IMAGING"
            ]
        assert receive(client, task, ["IMAGING"], key="imaging").status_code == 201
        assert current(client, task)["records_request"]["status"] == "RECEIVED"
        assert (await worker.execute_task(activation(fixture, task, 4)))["status"] == "COMPLETED"
        final = current(client, task)
        assert final["workflow_id"] == task["workflow_id"]
        assert final["progress_json"]["outstanding_requirement_ids"] == []
        assert all(r["evidence_source_ids"] for r in final["records_request"]["requirements"])
    finally:
        await database.close()


async def test_receipt_scope_validation_duplicate_delivery_and_no_early_completion(
    setup, database_url
):
    client, fixture, foreign = setup
    task, other = create(client, fixture), create(client, foreign)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        for payload in (
            {"status": "PARTIAL"},
            {
                "status": "PENDING",
                "received_requirement_ids": [task["records_request"]["requirements"][0]["id"]],
            },
            {"status": "PARTIAL", "received_requirement_ids": []},
            {
                "status": "PARTIAL",
                "received_requirement_ids": [other["records_request"]["requirements"][0]["id"]],
            },
        ):
            response = receive(client, task, **payload)
            assert response.status_code == 422, response.text
        assert receive(client, task, ["BILLS"], status="RECEIVED").status_code == 422
        first = receive(client, task, ["BILLS"])
        assert first.status_code == 201
        assert receive(client, task, ["IMAGING"]).status_code == 409
        assert receive(client, task, ["BILLS"], key="duplicate-delivery").status_code == 201
        assert receive(client, task, status="PENDING", key="processing").status_code == 201
        assert current(client, task)["records_request"]["status"] == "PARTIAL"
        await due(database, task)
        run = await worker._prepare(activation(fixture, task, 1))
        tool = next(t for t in worker._tools(run) if t.name == "check_request_status")
        from test_records_reminders import invoke

        result = await invoke(tool, {})
        decision = RecordsDecision(
            summary="Premature completion",
            observations=[],
            received_source_ids=result["received_source_ids"],
            next_action=NextAction(type="complete", reason="done", follow_up_seconds=None),
        )
        with pytest.raises(ValueError, match="every required item"):
            await worker._commit(run, activation(fixture, task, 1), decision, {})
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(RecordEvidence)
                    .where(
                        RecordEvidence.tenant_id == UUID(fixture["firm_id"]),
                    )
                )
                == 1
            )
    finally:
        await database.close()


async def test_partial_receipt_fences_inflight_work_and_preserves_callback_and_holds(
    setup, database_url
):
    from test_records_reminders import invoke

    from agents_platform.execution import StaleExecution

    client, fixture, _ = setup
    task = create(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        await due(database, task)
        run = await worker._prepare(activation(fixture, task, 1))
        at = datetime.now(timezone.utc) + timedelta(days=1)
        response = receive(client, task, ["BILLS"], callback_at=at.isoformat())
        assert response.status_code == 201, response.text
        reminder = next(t for t in worker._tools(run) if t.name == "send_records_reminder")
        with pytest.raises(StaleExecution):
            await invoke(reminder, {"occurrence": 1})
        assert (await worker._commit(run, activation(fixture, task, 1), None, {}))[
            "status"
        ] == "CONTINUE"
        assert datetime.fromisoformat(current(client, task)["records_request"]["callback_at"]) == at
        assert change(client, fixture, task).status_code == 200
        assert receive(client, task, ["RECORDS"], key="records").status_code == 201
        # Receipt does not clear the appointment or lift the independently stored hold.
        assert current(client, task)["records_request"]["callback_at"]
        assert receive(client, task, ["IMAGING"], key="imaging").status_code == 201
        assert current(client, task)["records_request"]["callback_at"] is None
        assert (await worker.execute_task(activation(fixture, task, 2)))["status"] == "BLOCKED"
    finally:
        await database.close()


async def test_human_authorized_followup_targets_only_missing_items(setup, database_url):
    from test_human_requests import decide, requests, set_due

    client, fixture, _ = setup
    task = create(client, fixture)
    database, worker = runtime(database_url)
    try:
        await worker.execute_task(activation(fixture, task))
        assert receive(client, task, ["BILLS"]).status_code == 201
        for cycle in range(1, 4):
            await due(database, task)
            result = await worker.execute_task(activation(fixture, task, cycle))
        assert result["status"] == "BLOCKED"
        request = requests(client, fixture, task)[0]
        assert decide(client, fixture, request).status_code == 200
        await set_due(database, task)
        assert (await worker.execute_task(activation(fixture, task, 4)))["status"] == "WAITING"
        async with database.sessions() as session:
            continuation = await session.scalar(
                select(HumanContinuation).where(
                    HumanContinuation.task_id == UUID(task["id"]),
                )
            )
            source = await session.get(Source, continuation.source_id)
            assert {r["record_type"] for r in source.content["requested_items"]} == {
                "RECORDS",
                "IMAGING",
            }
            assert "bills" not in source.content["message"]
    finally:
        await database.close()


async def test_receipt_evidence_already_in_domain_context_can_be_cited(setup, database_url):
    from test_records_reminders import invoke

    from agents_platform.task_contracts import Observation

    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        task = create(client, fixture)
        await worker.execute_task(activation(fixture, task))
        assert receive(client, task, ["BILLS"]).status_code == 201
        await due(database, task)
        run = await worker._prepare(activation(fixture, task, 1))
        evidence = next(
            r["evidence_source_ids"][0]
            for r in run.context["domain"]["state"]["records_request"]["requirements"]
            if r["record_type"] == "BILLS"
        )
        assert evidence in run.context["domain"]["evidence_source_ids"]
        reminder = next(t for t in worker._tools(run) if t.name == "send_records_reminder")
        await invoke(reminder, {"occurrence": 1})
        decision = RecordsDecision(
            summary="Bills received; awaiting the remaining items.",
            observations=[Observation(statement="Bills received", source_id=UUID(evidence))],
            received_source_ids=[UUID(evidence)],
            next_action=NextAction(type="wait", follow_up_seconds=60, reason="Await missing items"),
        )
        outcome = await worker._commit(run, activation(fixture, task, 1), decision, {})
        assert outcome["status"] == "WAITING"
    finally:
        await database.close()

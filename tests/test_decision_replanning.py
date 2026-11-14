"""Invalid model claims replan with saved feedback, then reach bounded human review."""

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_memory import activation, runtime
from test_task_persistence import database_url as database_url
from test_task_persistence import post
from test_task_persistence import setup as setup

from agents_platform.models import (
    Execution,
    ExecutionIncident,
    HumanRequest,
    RecordEvidence,
    ToolInvocation,
)
from agents_platform.task_contracts import NextAction, Observation, RecordsDecision

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Requires PostgreSQL"),
]


async def test_invalid_document_claim_keeps_saved_send_and_audits_rejection(
    setup, database_url, monkeypatch
):
    client, f, _ = setup
    task = post(client, f, body={**f["assignment"], "agent_version": 3}).json()
    db, worker = runtime(database_url)
    original = worker._decide

    async def wrong_document(run, tools):
        decision, usage = await original(run, tools)
        # A real tool's source exists, but it is a request result, not a document.
        decision.received_source_ids = [UUID(run.tools_used[0]["source_id"])]
        return decision, usage

    monkeypatch.setattr(worker, "_decide", wrong_document)
    try:
        outcome = await worker.execute_task(activation(f, task))
        assert outcome["status"] == "CONTINUE"
        async with db.sessions() as s:
            run = await s.scalar(select(Execution).where(Execution.task_id == UUID(task["id"])))
            assert run.usage_json["rejected_decision"]["received_source_ids"]
            assert "received document evidence" in run.usage_json["decision_validation_error"]
            assert run.decision_json["received_source_ids"] == []
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(RecordEvidence)
                    .where(RecordEvidence.tenant_id == UUID(f["firm_id"]))
                )
                == 0
            )
        monkeypatch.setattr(worker, "_decide", original)
        assert (await worker.execute_task(activation(f, task, 1)))["status"] == "WAITING"
        async with db.sessions() as s:
            assert (
                await s.scalar(
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
        await db.close()


async def test_repeated_unknown_citations_reach_actionable_recovery_after_correction(
    setup, database_url, monkeypatch
):
    client, f, _ = setup
    task = post(client, f, body={**f["assignment"], "agent_version": 3}).json()
    db, worker = runtime(database_url)

    async def invalid(run, tools):
        return RecordsDecision(
            summary="An unsupported claim",
            received_source_ids=[],
            observations=[Observation(statement="No evidence", source_id=uuid4())],
            next_action=NextAction(type="wait", follow_up_seconds=60, reason="Wait"),
        ), {}

    original = worker._decide
    monkeypatch.setattr(worker, "_decide", invalid)
    try:
        for cycle, expected in enumerate(("CONTINUE", "CONTINUE", "FAILED")):
            assert (await worker.execute_task(activation(f, task, cycle)))["status"] == expected
        async with db.sessions() as s:
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(HumanRequest)
                    .where(HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN")
                )
                == 0
            )
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(ExecutionIncident)
                    .where(ExecutionIncident.task_id == UUID(task["id"]))
                )
                == 1
            )
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.task_id == UUID(task["id"]))
                )
                == 0
            )
        from test_recovery_contact import fresh, incidents, next_activation, resolve

        incident = incidents(client, f)[0]
        assert incident["error_type"] == "DecisionValidationError"
        assert resolve(client, f, incident).status_code == 200
        monkeypatch.setattr(worker, "_decide", original)
        resumed = fresh(client, f, task)
        assert (await worker.execute_task(next_activation(resumed)))["status"] == "WAITING"
    finally:
        await db.close()


async def test_new_provider_information_does_not_exhaust_automatic_polling_budget(
    setup, database_url
):
    from test_partial_receipts import path
    from test_task_persistence import AUTH

    client, f, _ = setup
    # Version 2 polls without a reminder cadence; this isolates the shared budget.
    task = post(client, f, body={**f["assignment"], "agent_version": 2}).json()
    db, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(f, task)))["status"] == "WAITING"
        for cycle in range(1, 11):
            response = client.post(
                path(task) + "/provider-updates",
                headers={**AUTH, "Idempotency-Key": f"update-{cycle}"},
                json={"status": "PENDING", "message": f"Provider status update {cycle}."},
            )
            assert response.status_code == 201
            result = await worker.execute_task(activation(f, task, cycle))
            assert result["status"] == "WAITING", result
    finally:
        await db.close()


async def test_premature_tool_keeps_summary_and_does_not_poison_future_send(
    setup, database_url, monkeypatch
):
    from types import SimpleNamespace

    from agents import Runner, Usage
    from test_records_reminders import due, invoke

    client, f, _ = setup
    task = post(client, f, body={**f["assignment"], "agent_version": 3}).json()
    db, worker = runtime(database_url)

    async def premature(agent, context, **kwargs):
        tools = {t.name: t for t in agent.tools}
        await invoke(tools["send_records_request"], {})
        rejected = await invoke(tools["send_records_reminder"], {"occurrence": 1})
        assert rejected["status"] == "NOT_DUE" and not rejected["action_taken"]
        return SimpleNamespace(
            final_output=RecordsDecision(
                summary=(
                    "Previously gathered information is retained; the request is awaiting response."
                ),
                observations=[],
                received_source_ids=[],
                next_action=NextAction(type="wait", follow_up_seconds=60, reason="Await due date"),
            ),
            context_wrapper=SimpleNamespace(usage=Usage()),
        )

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Runner, "run", premature)
            assert (await worker.execute_task(activation(f, task)))["status"] == "WAITING"
        async with db.sessions() as s:
            run = await s.scalar(select(Execution).where(Execution.task_id == UUID(task["id"])))
            assert run.decision_json["summary"].startswith("Previously gathered information")
            assert run.usage_json["tool_rejections"][0]["status"] == "NOT_DUE"
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.logical_key == "records-reminder/1",
                    )
                )
                == 0
            )
        await due(db, task)
        assert (await worker.execute_task(activation(f, task, 1)))["status"] == "WAITING"
        async with db.sessions() as s:
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.logical_key == "records-reminder/1",
                    )
                )
                == 1
            )
    finally:
        await db.close()

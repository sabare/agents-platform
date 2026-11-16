"""Context capacity is a technical incident, not permission for more contact."""

import os
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_memory import activation, runtime
from test_recovery_contact import fresh, incidents, next_activation, resolve
from test_task_persistence import database_url as database_url
from test_task_persistence import post
from test_task_persistence import setup as setup

from agents_platform.models import Execution, HumanRequest, ToolInvocation

pytestmark = pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="PostgreSQL")


async def test_technical_overflow_preserves_effects_and_recovers_without_provider_approval(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "WAITING"
        turn = activation(fixture, task, 1)
        run = await worker._prepare(turn)
        run.context["domain"]["state"]["essential"] = "Required unsplittable state " * 10000
        async with database.sessions.begin() as session:
            row = await session.get(Execution, run.execution_id)
            row.context_json = run.context
        decision, usage = await worker._decide(run, worker._tools(run))
        assert decision is None and usage["context_budget_exceeded"]
        outcome = await worker._commit(run, turn, decision, usage)
        assert outcome["status"] == "FAILED"
        incident = incidents(client, fixture)[0]
        assert incident["category"] == "CONTEXT_BUDGET"
        assert incident["error_type"] == "ContextBudgetExceeded"
        assert incident["allowed_actions"] == ["RETRY", "CANCEL"]
        async with database.sessions() as session:
            row = await session.get(Execution, run.execution_id)
            assert row.status == "FAILED" and row.decision_json is None
            assert row.usage_json["context_budget"]["failure"]["over_budget_tokens"] > 0
            assert not await session.scalar(
                select(HumanRequest.id).where(
                    HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                )
            )
        recovered = resolve(client, fixture, incident)
        assert recovered.status_code == 200, recovered.text
        current = fresh(client, fixture, task)
        assert current["id"] == task["id"]
        assert (await worker.execute_task(next_activation(current)))["status"] == "WAITING"
        async with database.sessions() as session:
            sends = await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_request",
                )
            )
            assert sends == 1
    finally:
        await database.close()

"""A second typed domain proves the executor does not need a records request."""

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from agents import Runner, Usage
from agents.tool_context import ToolContext
from pydantic import Field, ValidationError
from sqlalchemy import func, select
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.db import Database
from agents_platform.domains import (
    AdapterRegistry,
    DomainSnapshot,
    DomainValidation,
    ToolResult,
    ToolSpec,
)
from agents_platform.execution import StaleExecution, TaskActivities
from agents_platform.medical_records import MedicalRecordsAdapter
from agents_platform.models import AgentVersion, Execution, Task, ToolInvocation
from agents_platform.task_contracts import AgentDecision, Contract, NextAction, Observation


class ReceiptArguments(Contract):
    reference: str = Field(min_length=1, max_length=60)


class ReceiptDecision(AgentDecision):
    confirmed_reference: str | None


class ReceiptAdapter:
    """Test-only reference confirmation, not a shipped client check-in workflow."""

    key, version = "receipt_test", 1
    task_type, input_schema, agent_key = "receipt.confirm", "receipt.v1", "receipt_test"
    output_schemas = ("receipt_result.v1",)
    decision_type = ReceiptDecision
    tools = (
        ToolSpec(
            "confirm_receipt",
            "Confirm a simulated reference.",
            "contact",
            ReceiptArguments,
            task_key="confirm-once",
        ),
    )

    async def load(self, session, task):
        assignment = ReceiptArguments.model_validate(task.input_json)
        return DomainSnapshot(state=assignment.model_dump())

    async def execute_tool(self, session, task, name, arguments):
        assert name == "confirm_receipt"
        return ToolResult({"reference": arguments.reference, "simulated": True})

    async def validate(self, session, task, context, decision, results, action):
        if action.type == "complete" and not any(
            r["reference"] == decision.confirmed_reference == task.input_json["reference"]
            for r in results
        ):
            raise ValueError("Receipt completion requires a matching tool result")
        return DomainValidation(action, {"confirmed_reference": decision.confirmed_reference})

    def review(self, reason):
        return ReceiptDecision(
            summary=reason,
            observations=[],
            confirmed_reference=None,
            next_action=NextAction(type="needs_human", follow_up_seconds=None, reason=reason),
        )

    def scripted_model(self, context, follow_up_seconds):
        return "test-double"  # Runner is patched; no external model request.


def definition():
    return {
        "agent_key": "receipt_test",
        "version": 1,
        "name": "Receipt test",
        "instructions": "test",
        "model": "test-double",
        "tools": ["confirm_receipt"],
        "output_schema": "receipt_result.v1",
        "capability_stage": "durable_demo",
        "limits": {"max_model_turns": 5, "max_tool_calls": 3, "wall_clock_seconds": 60},
    }


def test_registry_rejects_unknown_bindings_duplicate_tools_and_schema_mismatch():
    adapter = ReceiptAdapter()
    with pytest.raises(ValueError, match="Duplicate"):
        AdapterRegistry((adapter, adapter))
    registry = AdapterRegistry((adapter,))
    task = SimpleNamespace(
        task_type="receipt.confirm",
        input_schema="receipt.v1",
        agent_key="receipt_test",
        agent_version=1,
    )
    assert registry.for_execution(task, definition()) is adapter
    for modified in (
        {"output_schema": "records_decision.v1"},
        {"tools": ["send_records_request"]},
        {"version": 2},
    ):
        with pytest.raises(ValueError):
            registry.for_execution(task, {**definition(), **modified})
    with pytest.raises(ValueError, match="different domain adapter version"):
        registry.for_execution(task, definition(), {"key": "receipt_test", "version": 2})
    with pytest.raises(ValueError, match="Unsupported"):
        registry.for_task(
            SimpleNamespace(task_type="unknown", input_schema="unknown", agent_key="unknown")
        )
    with pytest.raises(ValidationError):
        ReceiptArguments.model_validate({"reference": "ref", "tenant_id": str(uuid4())})


integration = pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke")


async def create_receipt(database, fixture):
    async with database.sessions.begin() as session:
        row = await session.get(AgentVersion, ("receipt_test", 1))
        if not row:
            session.add(
                AgentVersion(
                    agent_key="receipt_test",
                    version=1,
                    definition_hash="test-receipt-v1",
                    definition_snapshot=definition(),
                )
            )
            await session.flush()
        task = Task(
            id=uuid4(),
            tenant_id=UUID(fixture["firm_id"]),
            case_id=UUID(fixture["assignment"]["case_id"]),
            kind="AGENT",
            task_type="receipt.confirm",
            objective="Confirm reference R-17",
            agent_key="receipt_test",
            agent_version=1,
            input_schema="receipt.v1",
            input_json={"reference": "R-17"},
            progress_json={},
            completion_json={},
            status="PENDING",
            workflow_id=f"receipt-{uuid4()}",
        )
        session.add(task)
        await session.flush()
        return task


def activation(task, cycle=0):
    return {
        "tenant_id": str(task.tenant_id),
        "task_id": str(task.id),
        "cycle": cycle,
        "trigger": "continue",
    }


def ctx(tool):
    return ToolContext(context=None, tool_name=tool.name, tool_call_id="test", tool_arguments="{}")


@pytest.mark.integration
@integration
async def test_second_domain_runs_tools_validation_and_commit_through_same_executor(
    setup, database_url, monkeypatch
):
    _, fixture, _ = setup
    database = Database(database_url)
    activities = TaskActivities(
        database,
        Settings(_env_file=None, agent_mode="scripted"),
        AdapterRegistry((MedicalRecordsAdapter(), ReceiptAdapter())),
    )
    task = await create_receipt(database, fixture)

    async def respond(agent, *args, **kwargs):
        assert agent.output_type is ReceiptDecision
        tool = agent.tools[0]
        import json

        result = json.loads(await tool.on_invoke_tool(ctx(tool), '{"reference":"R-17"}'))
        # Repeated calls reuse the persisted action.
        assert json.loads(await tool.on_invoke_tool(ctx(tool), '{"reference":"R-17"}')) == result
        with pytest.raises(ValueError, match="different arguments"):
            await tool.on_invoke_tool(ctx(tool), '{"reference":"R-18"}')
        return SimpleNamespace(
            final_output=ReceiptDecision(
                summary="Confirmed",
                confirmed_reference="R-17",
                observations=[
                    Observation(
                        statement="Simulated reference confirmed", source_id=result["source_id"]
                    )
                ],
                next_action=NextAction(
                    type="complete", follow_up_seconds=None, reason="Matching evidence"
                ),
            ),
            context_wrapper=SimpleNamespace(usage=Usage(requests=1)),
        )

    monkeypatch.setattr(Runner, "run", respond)
    try:
        outcome = await activities.execute_task(activation(task))
        assert outcome["status"] == "COMPLETED"
        assert await activities.execute_task(activation(task)) == outcome
        async with database.sessions() as session:
            run = await session.scalar(select(Execution).where(Execution.task_id == task.id))
            assert run.context_json["domain"] == {
                "adapter": {"key": "receipt_test", "version": 1},
                "state": {"reference": "R-17"},
            }
            assert "records_request" not in run.context_json["task"]
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.task_id == task.id)
                )
                == 1
            )
            invocation = await session.scalar(
                select(ToolInvocation).where(ToolInvocation.task_id == task.id)
            )
            assert invocation.arguments_json == {"reference": "R-17"}
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_second_domain_uses_shared_contact_holds_and_stale_fences(setup, database_url):
    client, fixture, _ = setup
    database = Database(database_url)
    worker = TaskActivities(
        database,
        Settings(_env_file=None, agent_mode="scripted"),
        AdapterRegistry((ReceiptAdapter(),)),
    )
    task = await create_receipt(database, fixture)
    try:
        run = await worker._prepare(activation(task))
        # Operator API is case-scoped and does not require domain-specific request tables.
        url = f"/v1/firms/{task.tenant_id}/cases/{task.case_id}/restrictions"
        response = client.post(
            url,
            headers={**AUTH, "Idempotency-Key": "hold"},
            json={"expected_version": 0, "text": "Pause case contact"},
        )
        assert response.status_code == 200, response.text
        tool = worker._tools(run)[0]
        with pytest.raises(StaleExecution):
            await tool.on_invoke_tool(ctx(tool), '{"reference":"R-17"}')
        assert (await worker._commit(run, activation(task), None, {}))["status"] == "CONTINUE"
        assert (await worker.execute_task(activation(task, 1)))["status"] == "BLOCKED"
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_missing_adapter_context_is_rebuilt_and_wrong_version_is_rejected(
    setup, database_url
):
    _, fixture, _ = setup
    database = Database(database_url)
    worker = TaskActivities(
        database,
        Settings(_env_file=None, agent_mode="scripted"),
        AdapterRegistry((ReceiptAdapter(),)),
    )
    task = await create_receipt(database, fixture)
    try:
        run = await worker._prepare(activation(task))
        async with database.sessions.begin() as session:
            saved = await session.get(Execution, run.execution_id)
            context = dict(saved.context_json)
            context.pop("domain")
            saved.context_json = context
        assert (await worker._prepare(activation(task)))["status"] == "CONTINUE"
        fresh = await worker._prepare(activation(task, 1))
        async with database.sessions.begin() as session:
            saved = await session.get(Execution, fresh.execution_id)
            saved.context_json = {
                **saved.context_json,
                "domain": {"adapter": {"key": "receipt_test", "version": 99}, "state": {}},
            }
        with pytest.raises(ValueError, match="different domain adapter version"):
            await worker._prepare(activation(task, 1))
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_adapter_cannot_bypass_completion_or_citation_validation(setup, database_url):
    _, fixture, _ = setup
    database = Database(database_url)
    worker = TaskActivities(
        database,
        Settings(_env_file=None, agent_mode="scripted"),
        AdapterRegistry((ReceiptAdapter(),)),
    )
    task = await create_receipt(database, fixture)
    try:
        run = await worker._prepare(activation(task))
        tool = worker._tools(run)[0]
        with pytest.raises(ValidationError):
            await tool.on_invoke_tool(ctx(tool), '{"reference":"R-17","tenant_id":"other"}')
        decision = ReceiptDecision(
            summary="Done",
            observations=[],
            confirmed_reference="R-17",
            next_action=NextAction(type="complete", follow_up_seconds=None, reason="Done"),
        )
        with pytest.raises(ValueError, match="matching tool result"):
            await worker._commit(run, activation(task), decision, {})
        decision.observations = [Observation(statement="Invented source", source_id=uuid4())]
        with pytest.raises(ValueError, match="evidence it was not given"):
            await worker._commit(run, activation(task), decision, {})
        async with database.sessions() as session:
            saved = await session.get(Task, task.id)
            assert saved.status == "RUNNING"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.task_id == task.id)
                )
                == 0
            )
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_platform_obligation_gate_applies_after_domain_validation(
    setup, database_url, monkeypatch
):
    _, fixture, _ = setup
    database = Database(database_url)
    task = await create_receipt(database, fixture)
    adapter = ReceiptAdapter()
    worker = TaskActivities(
        database, Settings(_env_file=None, agent_mode="scripted"), AdapterRegistry((adapter,))
    )

    async def premature_completion(*args):
        return DomainValidation(NextAction(type="complete", follow_up_seconds=None, reason="Done"))

    monkeypatch.setattr(adapter, "validate", premature_completion)
    try:
        async with database.sessions.begin() as session:
            current = await session.get(Task, task.id)
            current.progress_json = {
                "memory_items": [
                    {
                        "id": str(uuid4()),
                        "revision": 1,
                        "status": "OPEN",
                        "kind": "OBLIGATION",
                        "source_id": str(uuid4()),
                        "text": "Operator must verify receipt",
                    }
                ]
            }
        run = await worker._prepare(activation(task))
        result = await worker._commit(run, activation(task), adapter.review("Review"), {})
        assert result["status"] == "BLOCKED"
        assert "obligation" in result["reason"]
    finally:
        await database.close()

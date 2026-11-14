"""Discovery-to-completion checks for a third agent without supervisor-specific code."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from agents import Runner, Usage
from sqlalchemy import func, select
from test_domain_adapters import ReceiptAdapter, ReceiptArguments, ReceiptDecision, definition
from test_supervisor import activation, path, submit
from test_task_persistence import AUTH, connect
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.capabilities import (
    AgentCapability,
    CapabilityCatalog,
    DelegationScope,
    delegate_task,
    discover_capabilities,
)
from agents_platform.config import Settings
from agents_platform.coordination import SupervisorAction, SupervisorActivities, SupervisorDecision
from agents_platform.db import Database
from agents_platform.delegation import delegation_tools
from agents_platform.execution import TaskActivities
from agents_platform.models import ActivityEvent, OutboxMessage, Task
from agents_platform.registry import publish_definitions
from agents_platform.task_contracts import Contract, NextAction, Observation


def receipt_capability(**kwargs):
    return AgentCapability(
        definition=definition(),
        adapter=ReceiptAdapter(),
        assignment_type=ReceiptArguments,
        description="Confirm a supplied receipt reference and report the evidence.",
        **kwargs,
    )


def decision(actions=(), complete=False):
    return SupervisorDecision(
        summary="Coordinate receipt confirmation",
        evidence_event_ids=[],
        actions=list(actions),
        objective_satisfied=complete,
        next_review_at=None,
    )


def proposal(**kwargs):
    return SupervisorAction(
        type="delegate_task",
        target_index=None,
        question=None,
        agent_key="receipt_test",
        agent_version=1,
        task_objective="Confirm receipt R-17",
        assignment_json='{"reference":"R-17"}',
        **kwargs,
    )


def test_dropping_a_plugin_module_discovers_it_without_changing_a_central_list(
    tmp_path, monkeypatch
):
    package = tmp_path / "example_agent_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "receipt.py").write_text(
        "from test_domain_adapters import ReceiptAdapter, ReceiptArguments, definition\n"
        "from agents_platform.capabilities import AgentCapability\n"
        "CAPABILITIES = (AgentCapability(definition=definition(), adapter=ReceiptAdapter(),\n"
        " assignment_type=ReceiptArguments, description='Confirm receipt references.'),)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    catalog = discover_capabilities("example_agent_plugins")
    capability = catalog.capabilities["receipt_test"]
    assert capability.expose_to_supervisor is True
    assert (
        catalog.adapters.for_task(
            SimpleNamespace(
                task_type="receipt.confirm",
                input_schema="receipt.v1",
                agent_key="receipt_test",
            )
        )
        is capability.adapter
    )
    tools = delegation_tools(catalog, [capability.card()], [], SupervisorAction)
    assert [t.name for t in tools] == ["delegate_receipt_test"]
    schema = tools[0].params_json_schema
    assert schema["properties"]["assignment"]["$ref"].endswith("ReceiptArguments")
    assert "tenant_id" not in json.dumps(schema)


async def test_generated_tools_validate_typed_inputs_and_only_queue_proposals():
    capability = receipt_capability()
    catalog, proposals = CapabilityCatalog((capability,)), []
    tool = delegation_tools(catalog, [capability.card()], proposals, SupervisorAction)[0]
    valid = '{"objective":"Confirm R-17","assignment":{"reference":"R-17"}}'
    assert json.loads(await tool.on_invoke_tool(None, valid))["status"] == "PROPOSED"
    await tool.on_invoke_tool(None, valid)
    assert len(proposals) == 1
    bad = '{"objective":"Confirm R-17","assignment":{"reference":"R-17","tenant_id":"other"}}'
    assert "error" in json.loads(await tool.on_invoke_tool(None, bad))
    assert len(proposals) == 1


def test_exposure_opt_out_and_invalid_registration():
    hidden = receipt_capability(expose_to_supervisor=False)
    with pytest.raises(ValueError, match="exposed"):
        CapabilityCatalog((hidden,)).resolve("receipt_test", 1)
    with pytest.raises(ValueError, match="Duplicate"):
        CapabilityCatalog((hidden, hidden))

    class UnsafeAssignment(Contract):
        tenant_id: str

    with pytest.raises(ValueError, match="scope"):
        replace(hidden, assignment_type=UnsafeAssignment)
    with pytest.raises(ValueError, match="Unsupported task domain"):
        replace(hidden, definition={**definition(), "agent_key": "mismatched"})


integration = pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke")


async def published_catalog(database, capability=None):
    catalog = CapabilityCatalog((capability or receipt_capability(),))
    async with database.sessions.begin() as session:
        await publish_definitions(session, catalog)
    return catalog


@pytest.mark.integration
@integration
async def test_third_agent_is_discovered_delegated_executed_and_reported(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    database = Database(database_url)
    catalog = await published_catalog(database)
    from agents_platform import dashboard

    monkeypatch.setattr(dashboard, "discover_capabilities", lambda: catalog)
    workspace = client.get("/v1/operator/workspace", headers=AUTH)
    assert "receipt_test" in [a["key"] for a in workspace.json()["agents"]]
    supervisor = SupervisorActivities(
        database, Settings(_env_file=None, agent_mode="scripted"), catalog
    )
    worker = TaskActivities(
        database, Settings(_env_file=None, agent_mode="scripted"), catalog.adapters
    )
    group = submit(client, fixture, {"objective": "Confirm receipt reference R-17"}).json()[
        "group_task_id"
    ]

    async def respond(agent, prompt, **kwargs):
        context = json.loads(prompt)
        if agent.name == "Case Supervisor":
            tools = {t.name: t for t in agent.tools}
            assert "delegate_receipt_test" in tools
            assert context["capabilities"][0]["agent_key"] == "receipt_test"
            assert context["contacts"]
            if not context["tasks"]:
                args = '{"objective":"Confirm receipt R-17","assignment":{"reference":"R-17"}}'
                result = json.loads(await tools["delegate_receipt_test"].on_invoke_tool(None, args))
                assert result["status"] == "PROPOSED"
                # Calling the same delegation tool twice does not double-create work.
                await tools["delegate_receipt_test"].on_invoke_tool(None, args)
                output = decision()
            else:
                output = decision(
                    complete=all(t["status"] == "COMPLETED" for t in context["tasks"])
                )
        else:
            tool = agent.tools[0]
            result = json.loads(
                await tool.on_invoke_tool(
                    SimpleNamespace(
                        tool_name=tool.name, tool_call_id="receipt", tool_arguments="{}"
                    ),
                    '{"reference":"R-17"}',
                )
            )
            output = ReceiptDecision(
                summary="Confirmed receipt",
                confirmed_reference="R-17",
                observations=[
                    Observation(statement="Receipt confirmed", source_id=result["source_id"])
                ],
                next_action=NextAction(
                    type="complete", follow_up_seconds=None, reason="Evidence saved"
                ),
            )
        return SimpleNamespace(
            final_output=output, context_wrapper=SimpleNamespace(usage=Usage(requests=1))
        )

    monkeypatch.setattr(Runner, "run", respond)
    try:
        first = await supervisor.coordinate_case(activation(fixture))
        assert first["coordination_status"] == "MONITORING"
        assert await supervisor.coordinate_case(activation(fixture)) == first
        async with database.sessions() as session:
            tasks = (
                await session.scalars(
                    select(Task).where(
                        Task.tenant_id == UUID(fixture["firm_id"]),
                        Task.kind == "AGENT",
                    )
                )
            ).all()
            assert len(tasks) == 1
            task = tasks[0]
            assert task.parent_task_id == UUID(group)
            assert (task.agent_key, task.agent_version) == ("receipt_test", 1)
            assert task.input_json == {"reference": "R-17"}
            outbox = await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.destination_key == task.workflow_id,
                )
            )
            assert outbox.status == "PENDING"
        comment = client.post(
            f"/v1/operator/firms/{fixture['firm_id']}/tasks/{task.id}/comments",
            headers={**AUTH, "Idempotency-Key": "new-agent-comment"},
            json={
                "text": "Please confirm the receipt",
                "mentions": ["receipt_test"],
                "wake_agent": True,
            },
        )
        assert comment.status_code == 201, comment.text
        assert comment.json()["woke_agent"]
        outcome = await worker.execute_task(
            {
                "tenant_id": fixture["firm_id"],
                "task_id": str(task.id),
                "cycle": 0,
                "trigger": "created",
            }
        )
        assert outcome["status"] == "COMPLETED"
        result = await supervisor.coordinate_case(activation(fixture, 1))
        assert result["coordination_status"] == "SATISFIED"
        assert client.get(path(fixture), headers=AUTH).json()["plan"]["delegated_task_ids"] == [
            str(task.id)
        ]
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_catalog_filters_hidden_unpublished_and_changed_versions(setup, database_url):
    database = Database(database_url)
    capability = replace(receipt_capability(), definition={**definition(), "version": 99})
    try:
        async with database.sessions.begin() as session:
            assert await CapabilityCatalog((capability,)).available(session) == []
            await publish_definitions(session, CapabilityCatalog((capability,)))
            assert len(await CapabilityCatalog((capability,)).available(session)) == 1
            hidden = replace(capability, expose_to_supervisor=False)
            assert await CapabilityCatalog((hidden,)).available(session) == []
            changed = replace(
                capability, definition={**capability.definition, "instructions": "different"}
            )
            assert await CapabilityCatalog((changed,)).available(session) == []
            with pytest.raises(ValueError, match="Published agent version changed"):
                await publish_definitions(session, CapabilityCatalog((changed,)))
    finally:
        await database.close()


@pytest.mark.integration
@integration
@pytest.mark.parametrize("invalid", ["hidden", "unknown", "wrong_version", "wrong_input"])
async def test_invalid_delegation_batch_creates_no_tasks(setup, database_url, invalid):
    client, fixture, _ = setup
    database = Database(database_url)
    catalog = await published_catalog(database)
    supervisor = SupervisorActivities(
        database, Settings(_env_file=None, agent_mode="scripted"), catalog
    )
    submit(client, fixture, {"objective": "Confirm receipt reference R-17"})
    try:
        run, _ = await supervisor._prepare(activation(fixture))
        bad = proposal()
        if invalid == "hidden":
            supervisor.catalog = CapabilityCatalog(
                (receipt_capability(expose_to_supervisor=False),)
            )
        elif invalid == "unknown":
            bad.agent_key = "not_registered"
        elif invalid == "wrong_version":
            bad.agent_version = 99
        else:
            bad.assignment_json = '{"reference":"R-17","case_id":"other"}'
        result = await supervisor._commit(run, decision([proposal(), bad]), {})
        assert result["coordination_status"] == "NEEDS_REVIEW"
        with connect(database_url) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM tasks WHERE tenant_id=%s AND kind='AGENT'",
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 0
            )
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_delegation_retries_reuse_task_and_cannot_cross_case(setup, database_url):
    client, fixture, other = setup
    database = Database(database_url)
    catalog = await published_catalog(database)
    group = submit(client, fixture, {"objective": "Confirm receipt reference R-17"}).json()[
        "group_task_id"
    ]
    scope = DelegationScope(
        tenant_id=UUID(fixture["firm_id"]),
        case_id=UUID(fixture["assignment"]["case_id"]),
        parent_task_id=UUID(group),
        objective="Confirm receipt R-17",
        agent_version=1,
        actor_ref="agent/case_supervisor/3",
    )
    try:
        async with database.sessions.begin() as session:
            card = (await catalog.available(session))[0]
            task, created = await delegate_task(
                session, catalog, card, scope, {"reference": "R-17"}
            )
            duplicate, created_again = await delegate_task(
                session, catalog, card, scope, {"reference": "R-17"}
            )
            assert created and not created_again and task.id == duplicate.id
        from agents_platform.tasks import TaskError

        async with database.sessions.begin() as session:
            with pytest.raises(TaskError, match="same firm"):
                await delegate_task(
                    session,
                    catalog,
                    card,
                    replace(scope, case_id=UUID(other["assignment"]["case_id"])),
                    {"reference": "R-17"},
                )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ActivityEvent)
                    .where(
                        ActivityEvent.tenant_id == scope.tenant_id,
                        ActivityEvent.producer == "supervisor-delegation",
                    )
                )
                == 1
            )
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_assignment_planner_uses_catalog_and_approval_delegates_new_agent(
    setup, database_url, monkeypatch
):
    from agents_platform import assignment_router as routing
    from agents_platform import coordination

    client, fixture, _ = setup
    database = Database(database_url)
    catalog = await published_catalog(database)
    monkeypatch.setattr(routing, "discover_capabilities", lambda: catalog)
    monkeypatch.setattr(coordination, "discover_capabilities", lambda: catalog)

    async def interpret(settings, context):
        assert context["capabilities"][0]["agent_key"] == "receipt_test"
        return routing.AssignmentPlan(
            title="Confirm receipt",
            status="ready",
            summary="Confirm reference R-17",
            steps=[
                routing.AgentStep(
                    kind="agent",
                    agent_key="receipt_test",
                    agent_version=1,
                    objective="Confirm receipt R-17",
                    assignment_json='{"reference":"R-17"}',
                )
            ],
        ), {}

    monkeypatch.setattr(routing, "interpret_description", interpret)
    base = f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/assignments"
    try:
        proposed = client.post(
            base + "/plan",
            headers=AUTH,
            json={
                "description": "Confirm receipt R-17",
                "timezone": "UTC",
            },
        )
        assert proposed.status_code == 200, proposed.text
        assert proposed.json()["capabilities"][0]["name"] == "Receipt test"
        approved = client.post(base + f"/{proposed.json()['plan_id']}/approve", headers=AUTH)
        assert approved.status_code == 200, approved.text
        replay = client.post(base + f"/{proposed.json()['plan_id']}/approve", headers=AUTH)
        assert replay.json()["replayed"]
        supervisor = SupervisorActivities(
            database, Settings(_env_file=None, agent_mode="scripted"), catalog
        )
        assert (await supervisor.coordinate_case(activation(fixture)))[
            "coordination_status"
        ] == "MONITORING"
        async with database.sessions() as session:
            tasks = (
                await session.scalars(
                    select(Task).where(
                        Task.tenant_id == UUID(fixture["firm_id"]),
                        Task.kind == "AGENT",
                    )
                )
            ).all()
            assert len(tasks) == 1 and tasks[0].agent_key == "receipt_test"
    finally:
        await database.close()


@pytest.mark.integration
@integration
async def test_builtin_factory_delegation_deduplicates_and_checks_contacts(setup, database_url):
    from agents_platform.tasks import TaskError

    client, fixture, other = setup
    database = Database(database_url)
    catalog = discover_capabilities()
    group = submit(client, fixture, {"objective": "Collect approved records"}).json()[
        "group_task_id"
    ]
    scope = DelegationScope(
        tenant_id=UUID(fixture["firm_id"]),
        case_id=UUID(fixture["assignment"]["case_id"]),
        parent_task_id=UUID(group),
        objective="Collect approved records",
        agent_version=3,
        actor_ref="agent/case_supervisor/3",
    )
    payload = {
        key: fixture["assignment"][key]
        for key in (
            "patient_contact_id",
            "provider_contact_id",
            "requirements",
        )
    }
    try:
        async with database.sessions.begin() as session:
            card = next(
                c
                for c in await catalog.available(session)
                if c["agent_key"] == "medical_records_collector"
            )
            task, created = await delegate_task(session, catalog, card, scope, payload)
            again, repeated = await delegate_task(session, catalog, card, scope, payload)
            assert created and not repeated and again.id == task.id
        async with database.sessions.begin() as session:
            with pytest.raises(TaskError, match="contact must belong"):
                await delegate_task(
                    session,
                    catalog,
                    card,
                    scope,
                    {
                        **payload,
                        "provider_contact_id": other["assignment"]["provider_contact_id"],
                    },
                )
    finally:
        await database.close()

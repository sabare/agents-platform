"""Trace structure, payload redaction and a real SDK turn (no model network calls)."""

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import UUID

import pytest
from agents import Agent, FunctionTool, Model, ModelResponse, RunConfig, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform import observability as obs
from agents_platform.config import Settings
from agents_platform.db import Database
from agents_platform.execution import TaskActivities
from agents_platform.models import Execution, OutboxMessage


@pytest.fixture
def captured(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({"service.name": "test-observability"}))
    provider.add_span_processor(obs.IdentityProcessor())
    provider.add_span_processor(SimpleSpanProcessor(obs.SafeExporter(exporter)))
    telemetry = obs.Observability(provider=provider, settings=Settings(_env_file=None))
    monkeypatch.setattr(obs, "_runtime", telemetry)
    yield exporter
    provider.shutdown()


def test_export_boundary_removes_sensitive_fields_and_errors(captured):
    secret = "Jordan Lee jordan@example.com patient-history secret-key"
    with obs.observation("test", ai=True) as current:
        current.set_attribute("db.statement", "SELECT '" + secret + "'")
        current.set_attribute("url.full", "https://host/?secret=" + secret)
        current.set_attribute(
            "langfuse.observation.input", json.dumps({"text": secret, secret: secret})
        )
        current.record_exception(ValueError(secret))
        current.set_status(trace.Status(trace.StatusCode.ERROR, secret))
    exported = captured.get_finished_spans()[0]
    packet = json.dumps(dict(exported.attributes))
    assert "Jordan" not in packet and "secret-key" not in packet
    assert "db.statement" not in exported.attributes and "url.full" not in exported.attributes
    assert exported.events == () and exported.status.description is None
    assert exported.status.status_code == trace.StatusCode.ERROR
    assert exported.attributes["redaction.applied"] is True


def test_export_failure_does_not_change_business_result():
    class Broken(SpanExporter):
        def export(self, spans):
            raise RuntimeError("Backend down; do not expose error content")

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(obs.SafeExporter(Broken())))
    with provider.get_tracer("test").start_as_current_span("safe"):
        result = "business result"
    assert result == "business result"
    provider.shutdown()


class TwoTurnModel(Model):
    def __init__(self):
        self.turn = 0

    async def get_response(self, *args, **kwargs):
        self.turn += 1
        output = (
            [
                ResponseFunctionToolCall(
                    type="function_call",
                    name="check_status",
                    arguments='{"text":"private-health-data"}',
                    call_id="tool-1",
                    id="tool-1",
                )
            ]
            if self.turn == 1
            else [
                ResponseOutputMessage(
                    type="message",
                    id="answer",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text="private-answer", annotations=[]
                        )
                    ],
                )
            ]
        )
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
            response_id=None,
        )

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


async def test_real_sdk_model_and_tool_spans_have_one_trace_and_correct_parents(captured):
    async def callback(context, raw):
        return '{"status":"WAITING","text":"private-provider-response"}'

    tool = FunctionTool(
        name="check_status",
        description="Check status",
        params_json_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        on_invoke_tool=obs.observed_tool("check_status", callback),
    )
    with obs.activity_trace({"firm_id": "firm-1", "task_id": "task-1", "cycle": 0, "attempt": 2}):
        obs.bind_identity(**{"case.id": "case-1", "execution.id": "exec-1"})
        result = await obs.run_agent(
            Agent(name="test", model=TwoTurnModel(), tools=[tool]),
            "private-user-input",
            definition={"agent_key": "test", "version": 3, "model": "test-double"},
            mode="scripted",
            max_turns=3,
            run_config=RunConfig(tracing_disabled=True),
        )
        assert result.final_output == "private-answer"
    spans = captured.get_finished_spans()
    by_name = {s.name: s for s in spans}
    assert {
        "case",
        "task",
        "agent.execution",
        "agent.run",
        "llm.call",
        "tool.check_status",
    } <= by_name.keys()
    assert len([s for s in spans if s.name == "llm.call"]) == 2
    assert len({s.context.trace_id for s in spans}) == 1
    assert by_name["task"].parent.span_id == by_name["case"].context.span_id
    assert by_name["agent.execution"].parent.span_id == by_name["task"].context.span_id
    assert by_name["tool.check_status"].parent.span_id == by_name["agent.run"].context.span_id
    assert all(s.attributes.get("case.id") == "case-1" for s in spans)
    generations = [s for s in spans if s.name == "llm.call"]
    assert all(s.attributes["gen_ai.usage.total_tokens"] == 15 for s in generations)
    assert all(s.attributes["prompt.version"] == "3" for s in generations)
    assert "private-" not in json.dumps([dict(s.attributes) for s in spans])


async def test_parallel_activations_do_not_mix_identity(captured):
    async def execute(task_id):
        with obs.activity_trace({"task_id": task_id}):
            obs.bind_identity(**{"case.id": task_id + "-case"})
            await asyncio.sleep(0)
            with obs.observation("parallel.tool"):
                pass

    await asyncio.gather(execute("A"), execute("B"))
    for span in captured.get_finished_spans():
        assert span.attributes["case.id"] == span.attributes["task.id"] + "-case"


def test_evaluations_use_execution_observation_and_skip_stale(captured, monkeypatch):
    calls = []
    monkeypatch.setattr(
        obs._runtime, "langfuse", SimpleNamespace(create_score=lambda **kw: calls.append(kw))
    )
    with obs.activity_trace({"task_id": "task"}):
        obs.evaluation_scores({"status": "CONTINUE"})
        assert not calls
        obs.evaluation_scores({"status": "COMPLETED"})
    assert {c["name"] for c in calls} == {"backend_validation_passed", "task_completed"}
    assert all(c["value"] == 1 and c["observation_id"] for c in calls)


integration = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Requires PostgreSQL"
)


@pytest.mark.integration
@integration
async def test_saved_execution_contains_trace_with_context_sql_tools_and_result(
    setup, database_url, captured
):
    client, fixture, _ = setup
    response = post(client, fixture)
    assert response.status_code == 201, response.text
    task_id = UUID(response.json()["id"])
    database = Database(database_url)
    worker = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    try:
        result = await worker.execute_task(
            {
                "tenant_id": fixture["firm_id"],
                "task_id": str(task_id),
                "cycle": 0,
                "trigger": "created",
            }
        )
        assert result["status"] == "WAITING"
        async with database.sessions() as session:
            run = await session.scalar(select(Execution).where(Execution.task_id == task_id))
            saved = run.context_json["observability"]
            assert len(saved["trace_id"]) == 32 and saved["execution_span_id"]
            delivery = await session.scalar(
                select(OutboxMessage).where(
                    OutboxMessage.destination_key == response.json()["workflow_id"]
                )
            )
            assert delivery.payload["_trace"]["traceparent"]
        spans = [
            s
            for s in captured.get_finished_spans()
            if f"{s.context.trace_id:032x}" == saved["trace_id"]
        ]
        names = {s.name for s in spans}
        assert {
            "case",
            "task",
            "agent.execution",
            "context.assemble",
            "agent.run",
            "llm.call",
            "db.operation",
            "tool.send_records_request",
            "result.commit",
        } <= names
        assert all("db.statement" not in s.attributes for s in spans)
        assert "Example Hospital" not in json.dumps([dict(s.attributes) for s in spans])
        fetched = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task_id}/executions", headers=AUTH
        )
        assert fetched.json()[0]["observability"]["trace_id"] == saved["trace_id"]
    finally:
        await database.close()


@pytest.mark.parametrize("scope", ["legacy-targets", "approved-agent"])
async def test_supervisor_only_offers_delegation_tools_for_approved_scope(
    captured, monkeypatch, scope
):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from agents import Runner

    from agents_platform.capabilities import discover_capabilities
    from agents_platform.coordination import SupervisorActivities, SupervisorDecision
    from agents_platform.registry import load_definition

    catalog = discover_capabilities()
    context = {
        "objective": {"targets": [{}]}
        if scope == "legacy-targets"
        else {"approved_assignments": [{"agent_key": "client_check_in", "agent_version": 2}]},
        "plan": {},
        "new_events": [],
        "capabilities": [c.card() for c in catalog.capabilities.values()],
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    }
    offered = []

    async def respond(agent, input, **kwargs):
        offered.extend(tool.name for tool in agent.tools)
        return SimpleNamespace(
            final_output=SupervisorDecision(
                summary="Monitoring",
                evidence_event_ids=[],
                actions=[],
                objective_satisfied=False,
                next_review_at=None,
            ),
            context_wrapper=SimpleNamespace(usage=Usage(requests=1)),
        )

    monkeypatch.setattr(Runner, "run", respond)
    supervisor = SupervisorActivities(
        None, Settings(_env_file=None, agent_mode="scripted"), catalog
    )
    run = SimpleNamespace(context_json=context, mode="scripted", tenant_id=uuid4(), case_id=uuid4())
    await supervisor._decide(run, load_definition("case_supervisor", 3))
    assert "delegate_medical_records_collector" not in offered
    assert ("delegate_client_check_in" in offered) == (scope == "approved-agent")


@pytest.mark.integration
@integration
async def test_temporal_retry_keeps_trace_and_records_attempts_without_error_text(
    setup, database_url, captured, monkeypatch
):
    from uuid import uuid4

    from temporalio.client import Client
    from temporalio.contrib.opentelemetry import TracingInterceptor
    from temporalio.worker import Worker

    from agents_platform.dispatch import dispatch_once
    from agents_platform.orchestration.workflows import TaskWorkflow

    client, fixture, _ = setup
    response = post(client, fixture)
    task_id = UUID(response.json()["id"])
    database = Database(database_url)
    specialist = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    original = specialist._decide
    calls = 0

    async def flaky(run, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("private-provider-error")
        return await original(run, tools)

    monkeypatch.setattr(specialist, "_decide", flaky)
    temporal = await Client.connect(
        "localhost:7233", interceptors=[TracingInterceptor(tracer=obs.tracer())]
    )
    queue = "trace-retry-" + uuid4().hex
    try:
        async with Worker(
            temporal,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                specialist.execute_task,
                specialist.fail_task,
            ],
        ):
            await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
            for _ in range(150):
                async with database.sessions() as session:
                    from agents_platform.models import Task

                    task = await session.get(Task, task_id)
                    if task.status == "WAITING":
                        break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Retried activity did not reach WAITING")
            await temporal.get_workflow_handle(task.workflow_id).terminate("Disposable trace test")
        spans = captured.get_finished_spans()
        executions = [s for s in spans if s.name == "agent.execution"]
        assert {s.attributes.get("temporal.attempt") for s in executions} >= {1, 2}
        assert len({s.context.trace_id for s in executions}) == 1
        assert any(s.attributes.get("error.type") == "ConnectionError" for s in executions)
        assert "private-provider-error" not in json.dumps([dict(s.attributes) for s in spans])
    finally:
        await database.close()


def test_local_debug_content_preserves_model_io_but_keeps_jaeger_redacted(monkeypatch):
    settings = Settings(_env_file=None, langfuse_content_mode="local_debug")
    monkeypatch.setattr(obs, "_runtime", obs.Observability(settings=settings))
    payload = {
        "instructions": "Ask Jordan about treatment",
        "input": [{"content": "40 pages received"}],
        "pain_score": 7,
        "api_key": "never-export",
        "authorization_required": True,
    }
    captured = obs.trace_payload(payload)
    assert captured["instructions"] == payload["instructions"] and captured["pain_score"] == 7
    assert captured["api_key"] == "[REDACTED CREDENTIAL]"
    assert captured["authorization_required"] is True
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("llm.call") as span:
        span.set_attribute("langfuse.observation.input", json.dumps(captured))
    assert (
        "Ask Jordan" in obs.safe_span(span, readable=True).attributes["langfuse.observation.input"]
    )
    assert "Ask Jordan" not in obs.safe_span(span).attributes["langfuse.observation.input"]
    assert "never-export" not in json.dumps(captured)
    provider.shutdown()


def test_readable_content_requires_local_destination_and_environment():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            langfuse_content_mode="local_debug",
            langfuse_base_url="https://cloud.langfuse.com",
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, langfuse_content_mode="local_debug", otel_environment="production")

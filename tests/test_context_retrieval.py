"""Question-specific retrieval over months of history, through service/API/SDK paths."""

import json
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from agents import ModelResponse, Usage
from agents.tool_context import ToolContext
from openai.types.responses import ResponseFunctionToolCall
from sqlalchemy import select
from test_memory import activation, runtime
from test_memory_history import add_source, case_path, index_all
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.context import EvidenceRequest, RetrievalRequest, RetrievalService, tokens
from agents_platform.coordination import SupervisorActivities, SupervisorScriptedModel
from agents_platform.medical_records import MedicalRecordsAdapter, ScriptedModel
from agents_platform.memory_history import query_hash
from agents_platform.models import (
    ActivityEvent,
    CaseContact,
    Contact,
    Execution,
    MemoryPassage,
    MemoryQuery,
    Source,
    ToolInvocation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def service(database, settings, fixture):
    return RetrievalService(
        database, settings, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
    )


async def link_source(database, fixture, task, source_id, when):
    async with database.sessions.begin() as session:
        source = await session.get(Source, source_id)
        source.created_at = when
        session.add(
            ActivityEvent(
                tenant_id=UUID(fixture["firm_id"]),
                case_id=UUID(task["case_id"]),
                task_id=UUID(task["id"]),
                event_type="provider.note",
                producer="retrieval-test",
                dedupe_key=str(source_id),
                actor_ref="fixture",
                payload={"source_id": str(source_id)},
            )
        )


async def test_targeted_question_finds_march_detail_among_240_updates(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        # One case, seven months, many plausible but irrelevant administrative updates.
        async with database.sessions.begin() as session:
            for i in range(240):
                session.add(
                    Source(
                        tenant_id=UUID(fixture["firm_id"]),
                        case_id=UUID(task["case_id"]),
                        source_key=f"noise/{uuid4()}",
                        kind="DEMO_MESSAGE",
                        content={
                            "message": f"Office update {i}: mailing address and billing hours."
                        },
                        created_at=datetime(2026, 1 + i % 7, 15, tzinfo=timezone.utc),
                    )
                )
        target = add_source(
            database_url, fixture, {"message": "MRI reference ZQ731: missing signed release."}
        )
        later = add_source(
            database_url, fixture, {"message": "MRI reference ZQ731: signed release accepted."}
        )
        await link_source(
            database, fixture, task, target, datetime(2026, 3, 12, tzinfo=timezone.utc)
        )
        await link_source(
            database, fixture, task, later, datetime(2026, 5, 12, tzinfo=timezone.utc)
        )
        await index_all(database)
        packet = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(
                question="Why was MRI reference ZQ731 blocked?",
                provider_id=UUID(fixture["assignment"]["provider_contact_id"]),
                recorded_after="2026-03-01T00:00:00Z",
                recorded_before="2026-04-01T00:00:00Z",
                max_tokens=1200,
            )
        )
        assert [h["source_id"] for h in packet["results"]] == [str(target)]
        assert "missing signed release" in packet["results"][0]["text"]
        assert packet["results"][0]["event_ids"]
        assert tokens(packet) <= 1200
        assert packet["indexing"]["complete"]
        # The old broad objective has no matching terms in the target; focused retrieval does.
        broad = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(question="Collect all medical records")
        )
        assert str(target) not in {h["source_id"] for h in broad["results"]}
    finally:
        await database.close()


async def test_same_words_in_separate_reports_keep_both_dates_and_sources(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    content = {"message": "Provider requires a signed release."}
    march = add_source(database_url, fixture, content)
    may = add_source(database_url, fixture, content)
    database, worker = runtime(database_url)
    try:
        await link_source(
            database, fixture, task, march, datetime(2026, 3, 12, tzinfo=timezone.utc)
        )
        await link_source(database, fixture, task, may, datetime(2026, 5, 12, tzinfo=timezone.utc))
        await index_all(database)
        packet = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(question="signed release")
        )
        hits = {h["source_id"]: h for h in packet["results"]}
        assert set(hits) == {str(march), str(may)}
        assert hits[str(march)]["recorded_at"].startswith("2026-03-12")
        assert hits[str(may)]["recorded_at"].startswith("2026-05-12")
        assert hits[str(march)]["event_ids"] != hits[str(may)]["event_ids"]
        assert hits[str(march)]["event_ids"] and hits[str(may)]["event_ids"]
        assert not packet["truncated"]
    finally:
        await database.close()


async def test_api_auth_scope_provider_and_date_validation(setup, database_url):
    client, fixture, foreign = setup
    own = add_source(database_url, fixture, {"message": "Private signature discussion"})
    other = add_source(database_url, foreign, {"message": "Private signature discussion"})
    url = case_path(fixture) + "/context"
    assert client.post(url + "/retrieve", json={"question": "signature"}).status_code == 401
    assert (
        client.post(url + "/read", headers=AUTH, json={"source_id": str(other)}).status_code == 404
    )
    assert (
        client.post(
            url + "/retrieve",
            headers=AUTH,
            json={
                "question": "signature",
                "provider_id": foreign["assignment"]["provider_contact_id"],
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            url + "/retrieve",
            headers=AUTH,
            json={
                "question": "signature",
                "recorded_after": "2026-04-01T00:00:00Z",
                "recorded_before": "2026-03-01T00:00:00Z",
            },
        ).status_code
        == 422
    )
    # Exact lookup also obeys filters; it cannot bypass an excluded date range.
    response = client.post(
        url + "/retrieve",
        headers=AUTH,
        json={
            "question": str(own),
            "recorded_after": "2099-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    assert not response.json()["results"]


async def test_provider_filter_excludes_other_provider_in_same_case(setup, database_url):
    client, fixture, _ = setup
    first = post(client, fixture).json()
    database, worker = runtime(database_url)
    provider_id = uuid4()
    try:
        async with database.sessions.begin() as session:
            session.add(
                Contact(
                    id=provider_id,
                    tenant_id=UUID(fixture["firm_id"]),
                    display_name="Other Hospital",
                    kind="ORGANIZATION",
                )
            )
            await session.flush()
            session.add(
                CaseContact(
                    tenant_id=UUID(fixture["firm_id"]),
                    case_id=UUID(first["case_id"]),
                    contact_id=provider_id,
                    roles=["MEDICAL_PROVIDER"],
                )
            )
        second = post(
            client,
            fixture,
            key="other-provider",
            body={
                **fixture["assignment"],
                "provider_contact_id": str(provider_id),
            },
        ).json()
        own = add_source(database_url, fixture, {"message": "MRI blocked by signature"})
        other = add_source(database_url, fixture, {"message": "MRI blocked by signature"})
        when = datetime(2026, 3, 12, tzinfo=timezone.utc)
        await link_source(database, fixture, first, own, when)
        await link_source(database, fixture, second, other, when)
        await index_all(database)
        packet = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(
                question="MRI signature",
                provider_id=UUID(fixture["assignment"]["provider_contact_id"]),
            )
        )
        assert {h["source_id"] for h in packet["results"]} == {str(own)}
        # A better vector match in another provider must be filtered before ranking.
        query = "diagnostic pictures need paperwork"
        vector = [1.0] + [0.0] * 1535
        async with database.sessions.begin() as session:
            for sid, embedding in ((own, [0.0, 1.0] + [0.0] * 1534), (other, vector)):
                passage = await session.scalar(
                    select(MemoryPassage).where(MemoryPassage.source_id == sid)
                )
                passage.embedding = embedding
            session.add(
                MemoryQuery(
                    tenant_id=UUID(fixture["firm_id"]),
                    case_id=UUID(first["case_id"]),
                    query_hash=query_hash(query),
                    text=query,
                    embedding=vector,
                )
            )
        filtered = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(
                question=query,
                provider_id=UUID(fixture["assignment"]["provider_contact_id"]),
            )
        )
        assert filtered["method"] == "hybrid-rrf"
        assert {h["source_id"] for h in filtered["results"]} == {str(own)}
        exact = await service(database, worker.settings, fixture).retrieve(
            RetrievalRequest(
                question=str(other),
                provider_id=UUID(fixture["assignment"]["provider_contact_id"]),
            )
        )
        assert not exact["results"]
    finally:
        await database.close()


async def test_unindexed_and_changed_evidence_are_explicit_and_read_is_paginated(
    setup, database_url
):
    _, fixture, foreign = setup
    source_id = add_source(database_url, fixture, {"message": "Specific MRI detail. " * 1000})
    database, worker = runtime(database_url)
    try:
        retrieval = service(database, worker.settings, fixture)
        packet = await retrieval.retrieve(RetrievalRequest(question="Specific MRI"))
        assert packet["indexing"]["unindexed_sources"] >= 1
        assert packet["missing_information"]
        read = await retrieval.read(EvidenceRequest(source_id=source_id, max_tokens=400))
        assert tokens(read) <= 400
        assert read["truncated"] and read["next_offset"]
        next_page = await retrieval.read(
            EvidenceRequest(
                source_id=source_id,
                offset=read["next_offset"],
                expected_hash=read["source_hash"],
                max_tokens=400,
            )
        )
        assert next_page["offset"] == read["next_offset"]
        async with database.sessions.begin() as session:
            row = await session.get(Source, source_id)
            row.content = {"message": "Corrected MRI detail"}
        changed = await retrieval.read(
            EvidenceRequest(source_id=source_id, expected_hash=read["source_hash"])
        )
        assert "changed" in changed["error"]
        forbidden = await service(database, worker.settings, foreign).read(
            EvidenceRequest(source_id=source_id)
        )
        assert "error" in forbidden
    finally:
        await database.close()


def call(name, arguments, identifier):
    return ModelResponse(
        output=[
            ResponseFunctionToolCall(
                type="function_call",
                name=name,
                arguments=json.dumps(arguments),
                call_id=identifier,
                id=identifier,
            )
        ],
        usage=Usage(requests=1),
        response_id=None,
    )


class QuestionModel(ScriptedModel):
    def __init__(self, context, delay):
        super().__init__(context, delay)
        self.retrieval_turn = 0

    async def get_response(self, system_instructions, input, *args, **kwargs):
        self.retrieval_turn += 1
        if self.retrieval_turn == 1:
            return call("retrieve_context", {"question": "MRI signature"}, "retrieve-1")
        if self.retrieval_turn == 2:
            output = next(i for i in input if i.get("type") == "function_call_output")
            hit = json.loads(output["output"])["results"][0]
            return call(
                "read_evidence",
                {"source_id": hit["source_id"], "expected_hash": hit["source_hash"]},
                "read-1",
            )
        if self.retrieval_turn == 3:
            # The next SDK call can compare the search result with its expanded source.
            outputs = [i for i in input if i.get("type") == "function_call_output"]
            assert "MRI signature" in json.loads(outputs[0]["output"])["results"][0]["text"]
            assert "MRI signature" in json.loads(outputs[1]["output"])["text"]
        return await super().get_response(system_instructions, input, *args, **kwargs)


async def test_specialist_uses_both_tools_through_sdk_and_saves_audit(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    add_source(database_url, fixture, {"message": "MRI signature is required"})
    database, worker = runtime(database_url)
    monkeypatch.setattr(
        MedicalRecordsAdapter, "scripted_model", lambda self, c, d: QuestionModel(c, d)
    )
    try:
        await index_all(database)
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "WAITING"
        async with database.sessions() as session:
            calls = (
                await session.scalars(
                    select(ToolInvocation).where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name.in_(["retrieve_context", "read_evidence"]),
                    )
                )
            ).all()
            assert {c.tool_name for c in calls} == {"retrieve_context", "read_evidence"}
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]))
            )
            estimates = run.usage_json["context_budget"]["estimated_input_tokens_per_call"]
            assert len(estimates) == 6 and max(estimates) <= worker.settings.context_input_tokens
    finally:
        await database.close()


async def test_supervisor_uses_shared_retrieval_through_sdk(setup, database_url, monkeypatch):
    from test_supervisor import activation as case_activation
    from test_supervisor import submit

    client, fixture, _ = setup
    add_source(database_url, fixture, {"message": "MRI signature is required"})
    assert submit(client, fixture).status_code == 201
    database, worker = runtime(database_url)
    original = SupervisorScriptedModel.get_response

    async def question(self, *args, **kwargs):
        if not getattr(self, "retrieved", False):
            self.retrieved = True
            return call("retrieve_context", {"question": "MRI signature"}, "case-retrieve")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SupervisorScriptedModel, "get_response", question)
    try:
        await index_all(database)
        supervisor = SupervisorActivities(database, worker.settings)
        await supervisor.coordinate_case(case_activation(fixture))
        async with database.sessions() as session:
            saved = await session.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.tenant_id == UUID(fixture["firm_id"]),
                    ToolInvocation.tool_name == "retrieve_context",
                )
            )
            assert saved and saved.result_json["results"]
    finally:
        await database.close()


async def test_execution_scope_cannot_be_overridden_and_calls_are_bounded(setup, database_url):
    client, fixture, foreign = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        run = await worker._prepare(activation(fixture, task))
        tools = {t.name: t for t in worker._tools(run)}
        tool = tools["retrieve_context"]
        ctx = ToolContext(
            context=None, tool_name=tool.name, tool_call_id="test", tool_arguments="{}"
        )
        invalid = {"question": "MRI", "case_id": foreign["assignment"]["case_id"]}
        with pytest.raises(ValueError):
            await tool.on_invoke_tool(ctx, json.dumps(invalid))
        for _ in range(worker.settings.context_retrieval_calls):
            response = json.loads(await tool.on_invoke_tool(ctx, '{"question":"No MRI"}'))
        assert "limit reached" in response["error"]
    finally:
        await database.close()


async def test_essential_context_overflow_blocks_specialist_without_sending(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    worker.settings.context_input_tokens = 2048
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "BLOCKED"
        async with database.sessions() as session:
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]))
            )
            assert run.usage_json["context_budget_exceeded"]
            assert not await session.scalar(
                select(ToolInvocation.id).where(ToolInvocation.task_id == UUID(task["id"]))
            )
    finally:
        await database.close()

"""Case-wide restrictions, derived history and bounded recovery from invalid plans."""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import func, select
from test_memory import activation, change, path, runtime
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.execution import StaleExecution
from agents_platform.memory_history import (
    MemoryActivities,
    historical_context,
    latest_summary,
    query_hash,
    search_history,
)
from agents_platform.models import (
    MemoryPassage,
    MemoryQuery,
    Source,
    Task,
    ToolInvocation,
)
from agents_platform.task_contracts import NextAction, RecordsDecision

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def case_path(fixture):
    return f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}"


def case_change(client, fixture, key="case-hold", **overrides):
    current = client.get(case_path(fixture) + "/memory", headers=AUTH)
    assert current.status_code == 200, current.text
    return client.post(
        case_path(fixture) + "/restrictions",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "expected_version": current.json()["case_memory_version"],
            "text": "Fictional case-wide pause pending permission review.",
            **overrides,
        },
    )


def add_source(database_url, fixture, content, key=None, case_id=None):
    source_id = uuid4()
    with connect(database_url) as connection:
        from psycopg.types.json import Jsonb

        connection.execute(
            """INSERT INTO sources
            (id,tenant_id,case_id,source_key,kind,content,created_at)
            VALUES (%s,%s,%s,%s,'DEMO_MESSAGE',%s,now()-interval '9 months')""",
            (
                source_id,
                UUID(fixture["firm_id"]),
                UUID(case_id or fixture["assignment"]["case_id"]),
                key or f"test/{source_id}",
                Jsonb(content),
            ),
        )
    return source_id


async def index_all(database, settings=None):
    indexer = MemoryActivities(
        database, settings or Settings(_env_file=None, agent_mode="scripted")
    )
    for _ in range(10):
        result = await indexer.index_memory()
        if result["indexed_sources"] < 30:
            return result
    raise AssertionError("Test backlog did not drain")


async def test_case_hold_applies_to_existing_and_future_tasks_and_fences_inflight_send(
    setup, database_url
):
    client, fixture, _ = setup
    first = post(client, fixture).json()
    database, worker = runtime(database_url)
    try:
        stale = await worker._prepare(activation(fixture, first))
        hold = case_change(client, fixture)
        assert hold.status_code == 200, hold.text
        second = post(client, fixture, key="another-task").json()
        send = next(t for t in worker._tools(stale) if t.name == "send_records_request")
        with pytest.raises(StaleExecution):
            await send.on_invoke_tool(
                ToolContext(
                    context=None, tool_name=send.name, tool_call_id="test", tool_arguments="{}"
                ),
                "{}",
            )
        await worker._commit(stale, activation(fixture, first), None, {})
        for task, cycle in ((first, 1), (second, 0)):
            assert (await worker.execute_task(activation(fixture, task, cycle)))[
                "status"
            ] == "BLOCKED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(ToolInvocation.tenant_id == UUID(fixture["firm_id"]))
                )
                == 0
            )
        release = case_change(
            client,
            fixture,
            key="case-release",
            operation="RESOLVE",
            item_id=hold.json()["item_id"],
            text="Teammate verified permission.",
        )
        assert release.status_code == 200, release.text
        assert (await worker.execute_task(activation(fixture, second, 1)))["status"] == "WAITING"
    finally:
        await database.close()


def test_case_restrictions_are_scoped_idempotent_and_require_explicit_revision(setup):
    client, fixture, other = setup
    url = case_path(fixture) + "/restrictions"
    payload = {"expected_version": 0, "text": "Pause all contact"}
    headers = {**AUTH, "Idempotency-Key": "once"}
    assert client.post(url, json=payload).status_code in (401, 403)
    assert (
        client.post(
            url.replace(fixture["firm_id"], other["firm_id"]), headers=headers, json=payload
        ).status_code
        == 404
    )
    assert client.post(url, headers=headers, json=payload).status_code == 200
    assert client.post(url, headers=headers, json=payload).json()["replayed"]
    assert (
        client.post(url, headers={**AUTH, "Idempotency-Key": "stale"}, json=payload).status_code
        == 409
    )
    assert (
        case_change(
            client,
            fixture,
            key="foreign",
            provider_contact_id=other["assignment"]["provider_contact_id"],
        ).status_code
        == 422
    )


async def test_provider_hold_does_not_block_other_providers_or_get_lifted_by_task_resolution(
    setup, database_url
):
    client, fixture, _ = setup
    held = post(client, fixture).json()
    other_provider = uuid4()
    with connect(database_url) as connection:
        connection.execute(
            "INSERT INTO contacts(id,tenant_id,display_name,kind) "
            "VALUES(%s,%s,'Second Hospital','ORGANIZATION')",
            (other_provider, UUID(fixture["firm_id"])),
        )
        connection.execute(
            "INSERT INTO case_contacts(id,tenant_id,case_id,contact_id,roles) "
            "VALUES(%s,%s,%s,%s,ARRAY['MEDICAL_PROVIDER'])",
            (
                uuid4(),
                UUID(fixture["firm_id"]),
                UUID(fixture["assignment"]["case_id"]),
                other_provider,
            ),
        )
    other = post(
        client,
        fixture,
        key="second-provider",
        body={**fixture["assignment"], "provider_contact_id": str(other_provider)},
    ).json()
    assert (
        case_change(
            client, fixture, provider_contact_id=fixture["assignment"]["provider_contact_id"]
        ).status_code
        == 200
    )
    own = change(client, fixture, held).json()
    assert (
        change(
            client,
            fixture,
            held,
            key="task-release",
            operation="RESOLVE",
            kind=None,
            item_id=own["item_id"],
            text="Only this task hold is resolved.",
        ).status_code
        == 200
    )
    database, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(fixture, held)))["status"] == "BLOCKED"
        assert (await worker.execute_task(activation(fixture, other)))["status"] == "WAITING"
    finally:
        await database.close()


async def test_old_history_is_searchable_source_linked_and_restart_idempotent(setup, database_url):
    client, fixture, other = setup
    old = add_source(
        database_url,
        fixture,
        {"message": "Hospital rejected the authorization because a signature was missing."},
    )
    foreign = add_source(
        database_url, other, {"message": "Private authorization from another firm"}
    )
    database, _ = runtime(database_url)
    try:
        await index_all(database)
        async with database.sessions() as session:
            hits = await search_history(
                session,
                UUID(fixture["firm_id"]),
                UUID(fixture["assignment"]["case_id"]),
                "authorization",
            )
            assert str(old) in {h["source_id"] for h in hits["results"]}
            assert str(foreign) not in {h["source_id"] for h in hits["results"]}
            summary = await latest_summary(
                session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
            )
            assert summary["kind"] == "EXTRACTIVE"
            assert any(
                e["source_id"] == str(old) and "signature" in e["excerpt"]
                for e in summary["entries"]
            )
            revision = summary["revision"]
            ids = (
                await session.scalars(
                    select(MemoryPassage.id).where(MemoryPassage.source_id == old)
                )
            ).all()
        await database.close()
        database, _ = runtime(database_url)
        await index_all(database)
        async with database.sessions() as session:
            assert (
                await latest_summary(
                    session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
                )
            )["revision"] == revision
            assert (
                await session.scalars(
                    select(MemoryPassage.id).where(MemoryPassage.source_id == old)
                )
            ).all() == ids
        read = client.get(case_path(fixture) + f"/sources/{old}", headers=AUTH)
        assert "signature" in read.json()["content"]["message"]
        assert (
            client.get(case_path(fixture) + f"/sources/{foreign}", headers=AUTH).status_code == 404
        )
    finally:
        await database.close()


async def test_vector_lane_is_exactly_scoped_before_ranking_and_fuses_with_keywords(
    setup, database_url
):
    client, fixture, foreign = setup
    target = add_source(database_url, fixture, {"message": "Radiology is on physical disc only."})
    unrelated = add_source(database_url, fixture, {"message": "Office mailing address."})
    foreign_id = add_source(database_url, foreign, {"message": "Private radiology source"})
    sibling_case = uuid4()
    with connect(database_url) as connection:
        connection.execute(
            "INSERT INTO cases(id,tenant_id,display_number,title,status) "
            "VALUES(%s,%s,%s,'Private sibling case','OPEN')",
            (sibling_case, UUID(fixture["firm_id"]), f"SIB-{sibling_case}"),
        )
    sibling_id = add_source(
        database_url, fixture, {"message": "Private sibling radiology"}, case_id=str(sibling_case)
    )
    database, _ = runtime(database_url)
    query, vector = "diagnostic pictures on portable media", [1.0] + [0.0] * 1535
    try:
        await index_all(database)
        async with database.sessions.begin() as session:
            for sid in (target, unrelated, foreign_id, sibling_id):
                passage = await session.scalar(
                    select(MemoryPassage).where(MemoryPassage.source_id == sid)
                )
                passage.embedding = vector if sid != unrelated else [0.0, 1.0] + [0.0] * 1534
            session.add(
                MemoryQuery(
                    tenant_id=UUID(fixture["firm_id"]),
                    case_id=UUID(fixture["assignment"]["case_id"]),
                    query_hash=query_hash(query),
                    text=query,
                    embedding=vector,
                )
            )
        async with database.sessions() as session:
            hits = await search_history(
                session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"]), query
            )
            assert hits["method"] == "hybrid-rrf"
            assert hits["results"][0]["source_id"] == str(target)
            assert hits["results"][0]["lanes"] == ["vector"]
            assert not {str(foreign_id), str(sibling_id)} & {
                h["source_id"] for h in hits["results"]
            }
        # Synthetic orthogonal vectors test SQL isolation/ranking, not embedding quality.
        response = client.get(
            case_path(fixture) + "/memory/search", params={"q": query}, headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.json()["method"] == "hybrid-rrf"
    finally:
        await database.close()


async def test_embedding_failure_keeps_lexical_summary_and_mandatory_memory_available(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    source = add_source(database_url, fixture, {"message": "Authorization requires a signature."})
    task = post(client, fixture).json()
    change(client, fixture, task)
    database, worker = runtime(database_url)

    async def unavailable(*args):
        raise RuntimeError("Simulated embedding outage")

    monkeypatch.setattr("agents_platform.memory_history.embed", unavailable)
    try:
        result = await index_all(database)
        assert result["embedding_error"] == "RuntimeError"
        async with database.sessions() as session:
            history = await historical_context(
                session, UUID(fixture["firm_id"]), UUID(task["case_id"]), "authorization"
            )
            assert str(source) in {h["source_id"] for h in history["retrieval"]["results"]}
            assert history["summary"]
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "BLOCKED"
    finally:
        await database.close()


async def test_stale_passages_and_summaries_are_rejected_then_rebuilt(setup, database_url):
    _, fixture, _ = setup
    source = add_source(database_url, fixture, {"message": "Authorization was rejected."})
    database, _ = runtime(database_url)
    try:
        await index_all(database)
        async with database.sessions.begin() as session:
            row = await session.get(Source, source)
            row.content = {"message": "Corrected: authorization was accepted."}
        async with database.sessions() as session:
            assert not (
                await search_history(
                    session,
                    UUID(fixture["firm_id"]),
                    UUID(fixture["assignment"]["case_id"]),
                    "rejected",
                )
            )["results"]
            assert (
                await latest_summary(
                    session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
                )
                is None
            )
        await index_all(database)
        async with database.sessions() as session:
            summary = await latest_summary(
                session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
            )
            assert summary["revision"] == 2
            assert "accepted" in summary["entries"][0]["excerpt"]
    finally:
        await database.close()


async def test_waiting_on_draft_replans_twice_then_requests_review(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    decision = RecordsDecision(
        summary="Wait for provider",
        observations=[],
        received_source_ids=[],
        next_action=NextAction(type="wait", follow_up_seconds=30, reason="Waiting"),
    )
    try:
        for cycle in range(3):
            run = await worker._prepare(activation(fixture, task, cycle))
            assert not isinstance(run, dict)
            outcome = await worker._commit(
                run, activation(fixture, task, cycle), decision.model_copy(deep=True), {}
            )
            assert outcome["status"] == ("CONTINUE" if cycle < 2 else "BLOCKED")
        assert "unsent" in outcome["reason"]
        current = client.get(path(fixture, task), headers=AUTH).json()
        assert current["progress_json"]["draft_replans"] == 3
    finally:
        await database.close()


async def test_replan_feedback_can_recover_without_duplicate_send(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)
    decision = RecordsDecision(
        summary="Wait for provider",
        observations=[],
        received_source_ids=[],
        next_action=NextAction(type="wait", follow_up_seconds=30, reason="Waiting"),
    )
    try:
        run = await worker._prepare(activation(fixture, task))
        assert (await worker._commit(run, activation(fixture, task), decision, {}))[
            "status"
        ] == "CONTINUE"
        fresh = await worker._prepare({**activation(fixture, task, 1), "trigger": "event"})
        assert "DRAFT" in fresh.context["action_requirements"]["last_validation_feedback"]
        decision, usage = await worker._decide(fresh, worker._tools(fresh))
        assert (await worker._commit(fresh, activation(fixture, task, 1), decision, usage))[
            "status"
        ] == "WAITING"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolInvocation)
                    .where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_records_request",
                    )
                )
                == 1
            )
    finally:
        await database.close()


async def test_derived_indexing_resumes_after_temporal_worker_restart(setup, database_url):
    from temporalio.client import Client
    from temporalio.worker import Worker

    from agents_platform.orchestration.workflows import MemoryIndexWorkflow

    _, fixture, _ = setup
    first = add_source(database_url, fixture, {"message": "First historical authorization"})
    database, _ = runtime(database_url)
    client = await Client.connect("localhost:7233")
    queue, workflow_id = f"memory-{uuid4()}", f"memory-{uuid4()}"
    indexer = MemoryActivities(database, Settings(_env_file=None, agent_mode="scripted"))

    async def wait_indexed(source_id):
        async with asyncio.timeout(35):
            while True:
                async with database.sessions() as session:
                    if await session.scalar(
                        select(MemoryPassage.id).where(MemoryPassage.source_id == source_id)
                    ):
                        return
                await asyncio.sleep(0.1)

    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[MemoryIndexWorkflow],
            activities=[indexer.index_memory],
        ):
            await client.start_workflow(MemoryIndexWorkflow.run, id=workflow_id, task_queue=queue)
            await wait_indexed(first)
        second = add_source(database_url, fixture, {"message": "New source while worker offline"})
        async with Worker(
            client,
            task_queue=queue,
            workflows=[MemoryIndexWorkflow],
            activities=[indexer.index_memory],
        ):
            await wait_indexed(second)
        async with database.sessions() as session:
            summary = await latest_summary(
                session, UUID(fixture["firm_id"]), UUID(fixture["assignment"]["case_id"])
            )
            assert summary["source_count"] == 2
    finally:
        await client.get_workflow_handle(workflow_id).terminate()
        await database.close()


async def test_invalid_wait_replanning_is_durable_and_bounded_in_task_workflow(
    setup, database_url, monkeypatch
):
    from temporalio.client import Client
    from test_task_persistence import task_worker, wait_for_status

    from agents_platform.dispatch import dispatch_once
    from agents_platform.models import Execution

    client, fixture, _ = setup
    task = post(client, fixture).json()
    database, worker = runtime(database_url)

    async def always_wait(*args):
        return RecordsDecision(
            summary="Invalid wait",
            observations=[],
            received_source_ids=[],
            next_action=NextAction(type="wait", follow_up_seconds=60, reason="Waiting"),
        ), {}

    monkeypatch.setattr("agents_platform.execution.TaskActivities._decide", always_wait)
    temporal = await Client.connect("localhost:7233")
    queue = f"replan-{uuid4()}"
    try:
        async with task_worker(database, worker.settings, temporal, queue):
            await dispatch_once(database, temporal, queue, UUID(fixture["firm_id"]))
            assert await wait_for_status(database, task["id"], {"BLOCKED", "FAILED"}) == "BLOCKED"
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Execution)
                    .where(Execution.task_id == UUID(task["id"]))
                )
                == 3
            )
            saved = await session.get(Task, UUID(task["id"]))
            assert saved.progress_json["draft_replans"] == 3
    finally:
        await database.close()


async def test_exact_source_lookup_works_before_background_indexing(setup, database_url):
    client, fixture, other = setup
    source = add_source(database_url, fixture, {"message": "Unindexed historical source"})
    database, _ = runtime(database_url)
    try:
        async with database.sessions() as session:
            hits = await search_history(
                session,
                UUID(fixture["firm_id"]),
                UUID(fixture["assignment"]["case_id"]),
                str(source),
            )
            assert hits["method"] == "exact"
            assert hits["results"][0]["source_id"] == str(source)
            assert hits["results"][0]["passage_id"] is None
            forbidden = await search_history(
                session, UUID(other["firm_id"]), UUID(other["assignment"]["case_id"]), str(source)
            )
            assert not forbidden["results"]
    finally:
        await database.close()


async def test_both_agents_save_source_linked_history_in_their_context(setup, database_url):
    from test_supervisor import activation as supervisor_activation
    from test_supervisor import submit

    from agents_platform.coordination import SupervisorActivities

    client, fixture, _ = setup
    source = add_source(
        database_url,
        fixture,
        {"message": "Example Hospital rejected medical records authorization; obtain signature."},
    )
    task = post(client, fixture).json()
    assert submit(client, fixture).status_code == 201
    database, worker = runtime(database_url)
    try:
        await index_all(database)
        run = await worker._prepare(activation(fixture, task))
        assert str(source) in {
            h["source_id"] for h in run.context["history"]["retrieval"]["results"]
        }
        supervisor = SupervisorActivities(database, worker.settings)
        coordinated, _ = await supervisor._prepare(supervisor_activation(fixture))
        assert str(source) in {
            h["source_id"] for h in coordinated.context_json["history"]["summary"]["entries"]
        }
        assert "never permission" in run.context["history"]["authority"]
    finally:
        await database.close()


async def test_exact_search_api_does_not_call_embedding_provider(setup, database_url, monkeypatch):
    client, fixture, _ = setup
    source = add_source(database_url, fixture, {"message": "Exact evidence lookup"})

    async def forbidden(*args):
        raise AssertionError("Exact lookups must not request embeddings")

    monkeypatch.setattr("agents_platform.memory_api.prepare_query", forbidden)
    response = client.get(
        case_path(fixture) + "/memory/search", params={"q": str(source)}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json()["method"] == "exact"

"""Opt-in interviewer journeys: real SDK/model calls, disposable PostgreSQL, simulated outreach.

RUN_INTEGRATION_TESTS=1 RUN_LIVE_AGENT_TESTS=1 pytest -x -v tests/test_live_journeys.py
No fixtures are added to the user's application database. API keys are never printed.
"""

import asyncio
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from agents.models import openai_provider
from dotenv import dotenv_values
from openai import DefaultAsyncHttpxClient
from sqlalchemy import delete, func, select
from temporalio.client import Client
from test_check_in import create as create_checkin
from test_check_in import reply
from test_client_reviews import decide
from test_context_retrieval import link_source
from test_memory import activation, change
from test_memory_history import add_source, index_all
from test_partial_receipts import create, current, path, receive
from test_records_reminders import due
from test_recovery_contact import (
    contacts_body,
    decision,
    failed_after_send,
    fresh,
    next_activation,
    resolve,
)
from test_supervisor import activation as case_activation
from test_task_persistence import AUTH, post, task_worker, wait_for_status
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.config import Settings
from agents_platform.context import RetrievalRequest, RetrievalService
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import (
    ActivityEvent,
    CaseContact,
    Contact,
    Execution,
    MemoryQuery,
    RecordEvidence,
    Task,
    ToolInvocation,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION_TESTS") != "1" or os.getenv("RUN_LIVE_AGENT_TESTS") != "1",
        reason="Opt-in live model journeys (API cost); use the documented command",
    ),
]


@pytest.fixture
def live(setup, database_url, monkeypatch):
    key = os.getenv("OPENAI_API_KEY") or dotenv_values(".env").get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY required")
    monkeypatch.setenv("OPENAI_API_KEY", key)
    # TestClient's API and the in-process worker use different event loops. Production
    # runs them in separate processes; avoid sharing the SDK's socket pool in this harness.
    monkeypatch.setattr(openai_provider, "shared_http_client", DefaultAsyncHttpxClient)
    client, fixture, foreign = setup
    monkeypatch.setattr(client.app.state.settings, "agent_mode", "openai")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        agent_mode="openai",
        demo_follow_up_seconds=60,
    )
    return client, fixture, foreign, settings


def checked(response):
    assert response.status_code < 300, response.text
    return response.json()


def assignment_root(fixture):
    return f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}/assignments"


async def second_provider(db, fixture):
    provider = uuid4()
    async with db.sessions.begin() as s:
        s.add(
            Contact(
                id=provider,
                tenant_id=UUID(fixture["firm_id"]),
                display_name="Riverside Medical Center",
                kind="ORGANIZATION",
            )
        )
        await s.flush()
        s.add(
            CaseContact(
                tenant_id=UUID(fixture["firm_id"]),
                case_id=UUID(fixture["assignment"]["case_id"]),
                contact_id=provider,
                roles=["MEDICAL_PROVIDER"],
            )
        )
    return provider


async def sends(db, task_id):
    async with db.sessions() as s:
        return await s.scalar(
            select(func.count())
            .select_from(ToolInvocation)
            .where(
                ToolInvocation.task_id == UUID(task_id),
                ToolInvocation.logical_key == "initial-send",
            )
        )


async def settle(worker, fixture, task, cycle=0):
    """Follow automatic continuation exactly as the durable workflow does."""
    for _ in range(4):
        async with worker.database.sessions() as session:
            latest = await session.scalar(
                select(func.max(Execution.cycle)).where(Execution.task_id == UUID(task["id"]))
            )
        cycle = max(cycle, latest + 1 if latest is not None else 0)
        request = activation(fixture, task, cycle)
        request["generation"] = task.get("progress_json", {}).get("recovery_generation", 0)
        if request["generation"]:
            request["trigger"] = "recovery"
        outcome = await worker.execute_task(request)
        if outcome["status"] != "CONTINUE":
            return outcome
        cycle += 1
    raise AssertionError("Task did not settle after bounded automatic replanning")


async def test_01_two_hospitals_plan_delegate_and_start_autonomously(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    try:
        second = await second_provider(db, f)
        root = assignment_root(f)
        draft = checked(
            client.post(
                root + "/plan",
                headers=AUTH,
                json={
                    "description": (
                        "Collect bills and records for all dates from Example Hospital "
                        "and Riverside Medical Center."
                    ),
                    "timezone": "Asia/Calcutta",
                },
            )
        )
        plan = draft["plan"]
        assert plan["status"] == "ready", plan
        assert plan["title"]
        targets = plan["steps"][0]["targets"]
        assert len(targets) == 2
        assert {t["provider_contact_id"] for t in targets} == {
            f["assignment"]["provider_contact_id"],
            str(second),
        }
        assert all(
            {r["record_type"] for r in t["requirements"]} == {"BILLS", "RECORDS"} for t in targets
        )
        approved = checked(client.post(root + f"/{draft['plan_id']}/approve", headers=AUTH))
        assert checked(client.post(root + f"/{draft['plan_id']}/approve", headers=AUTH))["replayed"]
        supervisor = SupervisorActivities(db, settings)
        outcome = await supervisor.coordinate_case(case_activation(f))
        assert outcome["coordination_status"] == "MONITORING", outcome
        async with db.sessions() as s:
            rows = (
                await s.scalars(
                    select(Task).where(Task.tenant_id == UUID(f["firm_id"]), Task.kind == "AGENT")
                )
            ).all()
            assert len(rows) == 2
            assert all(t.progress_json.get("display_title") for t in rows)
            tasks = [{"id": str(t.id)} for t in rows]
        worker = TaskActivities(db, settings)
        for task in tasks:
            result = await settle(worker, f, task)
            assert result["status"] == "WAITING", result
            assert await sends(db, task["id"]) == 1
        assert len(approved["tasks"]) == 1  # One parent objective, two durable specialists.
    finally:
        await db.close()


async def test_02_ambiguity_and_unsupported_work_do_not_start_partial_tasks(live):
    client, f, _, _ = live
    root = assignment_root(f)
    for description in (
        "Collect records from a hospital called Never Listed Hospital.",
        "Collect bills from Example Hospital and email the actual PDFs to outside@example.com.",
    ):
        draft = checked(
            client.post(root + "/plan", headers=AUTH, json={"description": description})
        )
        plan = draft["plan"]
        assert plan["status"] in {"needs_clarification", "unsupported"}, plan
        assert plan["questions"] and not plan["steps"]
        assert client.post(root + f"/{draft['plan_id']}/approve", headers=AUTH).status_code == 409
    db = Database(client.app.state.settings.database_url)
    try:
        async with db.sessions() as s:
            assert not (
                await s.scalars(
                    select(Task).where(Task.tenant_id == UUID(f["firm_id"]), Task.kind == "AGENT")
                )
            ).all()
    finally:
        await db.close()


async def test_03_bills_then_missing_only_followup_then_evidence_completion(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    worker = TaskActivities(db, settings)
    try:
        task = create(client, f)
        assert (await settle(worker, f, task))["status"] == "WAITING"
        body = receive(client, task, ["BILLS"])
        checked(body)
        assert receive(client, task, ["BILLS"]).json()["replayed"]
        states = {
            r["record_type"]: r["status"]
            for r in current(client, task)["records_request"]["requirements"]
        }
        assert states == {"BILLS": "RECEIVED", "RECORDS": "OUTSTANDING", "IMAGING": "OUTSTANDING"}
        await due(db, task)  # Advance the business due date; do not wait days in a test.
        outcome = await settle(worker, f, task, 1)
        assert outcome["status"] == "WAITING", outcome
        async with db.sessions() as s:
            reminder = await s.scalar(
                select(ToolInvocation).where(
                    ToolInvocation.task_id == UUID(task["id"]),
                    ToolInvocation.tool_name == "send_records_reminder",
                )
            )
            assert reminder is not None
            assert {r["record_type"] for r in reminder.result_json["requested_items"]} == {
                "RECORDS",
                "IMAGING",
            }
        checked(receive(client, task, ["RECORDS", "IMAGING"], status="RECEIVED", key="remaining"))
        outcome = await settle(worker, f, task, 2)
        assert outcome["status"] == "COMPLETED", outcome
        assert await sends(db, task["id"]) == 1
        async with db.sessions() as s:
            assert (
                await s.scalar(
                    select(func.count())
                    .select_from(RecordEvidence)
                    .where(RecordEvidence.tenant_id == UUID(f["firm_id"]))
                )
                == 3
            )
        events = checked(
            client.get(
                f"/v1/operator/firms/{f['firm_id']}/activity?task_id={task['id']}", headers=AUTH
            )
        )["items"]
        assert any(
            e["source_actor"] and e["source_actor"]["name"] == "Example Hospital"
            for e in events
            if e["type"] == "provider.response_received"
        )
    finally:
        await db.close()


async def test_04_hold_blocks_contact_and_resolution_resumes_same_task(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    worker = TaskActivities(db, settings)
    try:
        task = checked(post(client, f, body={**f["assignment"], "agent_version": 3}))
        hold = checked(change(client, f, task))
        assert (await settle(worker, f, task))["status"] == "BLOCKED"
        assert await sends(db, task["id"]) == 0
        checked(
            change(
                client,
                f,
                task,
                key="resolve",
                operation="RESOLVE",
                kind=None,
                item_id=hold["item_id"],
                text="User confirmed renewed authorization.",
            )
        )
        # Follow the same automatic CONTINUE transitions as TaskWorkflow. A model
        # may need the backend's validation feedback after an old hold is lifted.
        for cycle in range(1, 5):
            result = await worker.execute_task(activation(f, task, cycle))
            if result["status"] != "CONTINUE":
                break
        assert result["status"] == "WAITING", result
        assert current(client, task)["id"] == task["id"]
        assert current(client, task)["workflow_id"] == task["workflow_id"]
        assert await sends(db, task["id"]) == 1
    finally:
        await db.close()


async def test_05_failed_execution_manual_contact_and_safe_recovery(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    worker = TaskActivities(db, settings)
    try:
        task, incident = await failed_after_send(client, f, worker)  # Inject a post-send failure.
        assert incident["saved_actions"]
        body = decision(incident, contact=contacts_body(f))
        checked(resolve(client, f, incident, body))
        assert checked(resolve(client, f, incident, body))["replayed"]
        resumed = fresh(client, f, task)
        assert resumed["id"] == task["id"] and resumed["workflow_id"] != task["workflow_id"]
        assert (await worker.execute_task(activation(f, task)))["status"] == "CANCELLED"
        checked(
            client.post(
                path(task) + "/provider-updates",
                headers={**AUTH, "Idempotency-Key": "ready"},
                json={"status": "RECEIVED", "message": "All requested items delivered."},
            )
        )
        result = await settle(worker, f, resumed, next_activation(resumed)["cycle"])
        assert result["status"] == "COMPLETED", result
        assert await sends(db, task["id"]) == 1
    finally:
        await db.close()


async def test_06_client_concern_review_and_same_task_completion(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    worker = TaskActivities(db, settings)
    try:
        task = checked(create_checkin(client, f))
        assert (await settle(worker, f, task))["status"] == "WAITING"
        checked(reply(client, task, pain_score=9, requests_help=True, message="I need help today."))
        result = await settle(worker, f, task, 1)
        assert result["status"] == "BLOCKED", result
        blocked = current(client, task)
        checked(decide(client, blocked))
        result = await settle(worker, f, task, 2)
        assert result["status"] == "COMPLETED", result
        assert current(client, task)["id"] == task["id"]
        async with db.sessions() as s:
            names = (
                await s.scalars(
                    select(ToolInvocation.tool_name).where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "send_client_check_in",
                    )
                )
            ).all()
            assert len(names) == 1
    finally:
        await db.close()


async def test_07_semantic_history_retrieval_finds_named_contact_with_provenance(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    worker = TaskActivities(db, settings)
    try:
        task = checked(
            post(
                client,
                f,
                body={
                    **f["assignment"],
                    "agent_version": 3,
                    "objective": (
                        "Identify the individual employee responsible at this hospital. "
                        "Use retrieve_context to find their full name in older correspondence "
                        "before sending the records request. "
                        "Include the person's name in your summary."
                    ),
                },
            )
        )
        target = add_source(
            settings.database_url,
            f,
            {
                "message": (
                    "For disclosure paperwork, speak to Sofia Patel, "
                    "the release-of-information desk supervisor."
                )
            },
        )
        await link_source(db, f, task, target, datetime(2026, 2, 1, tzinfo=timezone.utc))
        async with db.sessions.begin() as s:
            event = await s.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.payload["source_id"].astext == str(target)
                )
            )
            event.created_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
        for i in range(45):
            noise = add_source(
                settings.database_url,
                f,
                {
                    "message": (
                        f"Administrative update {i}: billing address "
                        "and office parking details unchanged."
                    )
                },
            )
            await link_source(db, f, task, noise, datetime.now(timezone.utc))
        await index_all(db, settings)
        # Exercise on-demand retrieval when a new objective's cached embedding is
        # not ready yet. Passages retain real embeddings; initial keyword context
        # and recent-event windows must not accidentally hand the agent the answer.
        async with db.sessions.begin() as s:
            await s.execute(
                delete(MemoryQuery).where(
                    MemoryQuery.tenant_id == UUID(f["firm_id"]),
                    MemoryQuery.case_id == UUID(task["case_id"]),
                    MemoryQuery.text == task["objective"],
                )
            )
        service = RetrievalService(db, settings, UUID(f["firm_id"]), UUID(task["case_id"]))
        hits = await service.retrieve(
            RetrievalRequest(
                question="Who should we phone about the disclosure paperwork?",
                provider_id=UUID(f["assignment"]["provider_contact_id"]),
            )
        )
        assert hits["method"] == "hybrid-rrf", hits
        assert str(target) in {h["source_id"] for h in hits["results"]}
        result = await settle(worker, f, task)
        assert result["status"] == "WAITING", result
        async with db.sessions() as s:
            run = await s.scalar(select(Execution).where(Execution.task_id == UUID(task["id"])))
            initial_ids = {source["id"] for source in run.context_json["sources"]}
            initial_ids.update(
                h["source_id"] for h in run.context_json["history"]["retrieval"]["results"]
            )
            assert str(target) not in initial_ids
            tools = (
                await s.scalars(
                    select(ToolInvocation).where(
                        ToolInvocation.task_id == UUID(task["id"]),
                        ToolInvocation.tool_name == "retrieve_context",
                    )
                )
            ).all()
            assert "Sofia Patel" in run.decision_json["summary"], {
                "decision": run.decision_json,
                "retrievals": [
                    {"arguments": t.arguments_json, "result": t.result_json} for t in tools
                ],
            }
            assert tools and any(
                str(target) in {h["source_id"] for h in t.result_json.get("results", [])}
                for t in tools
            )
    finally:
        await db.close()


async def test_08_durable_wait_worker_restart_and_completion(live):
    client, f, _, settings = live
    db = Database(settings.database_url)
    temporal = await Client.connect("localhost:7233")
    queue = f"live-review-{uuid4()}"
    task = checked(post(client, f, body={**f["assignment"], "agent_version": 3}))
    try:
        async with task_worker(db, settings, temporal, queue):
            await dispatch_once(db, temporal, queue, UUID(f["firm_id"]))
            assert (
                await wait_for_status(db, task["id"], {"WAITING", "FAILED", "BLOCKED"}, timeout=100)
                == "WAITING"
            )
        # Save an update while no worker consumes this queue.
        checked(
            client.post(
                path(task) + "/provider-updates",
                headers={**AUTH, "Idempotency-Key": "after-restart"},
                json={
                    "status": "RECEIVED",
                    "message": "All records delivered while worker was offline.",
                },
            )
        )
        async with task_worker(db, settings, temporal, queue):
            while await dispatch_once(db, temporal, queue, UUID(f["firm_id"])):
                pass
            result = await asyncio.wait_for(
                temporal.get_workflow_handle(task["workflow_id"]).result(), 100
            )
            assert result["status"] == "COMPLETED", result
        assert await sends(db, task["id"]) == 1
        assert current(client, task)["workflow_id"] == task["workflow_id"]
    finally:
        await db.close()

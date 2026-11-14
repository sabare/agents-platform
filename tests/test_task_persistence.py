"""Real PostgreSQL tests in a disposable database, never the application database."""

import asyncio
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.api import create_app
from agents_platform.config import Settings
from agents_platform.db import Database
from agents_platform.seed import seed_demo

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]

AUTH = {"Authorization": "Bearer test-operator"}


@pytest.fixture(scope="module")
def database_url():
    from sqlalchemy.engine import make_url

    admin_url = make_url(
        os.getenv(
            "TEST_POSTGRES_ADMIN_URL", "postgresql+psycopg://agents:agents@localhost:5432/agents"
        )
    )
    database_name = f"agents_test_{uuid4().hex[:16]}"
    admin_connection = admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_connection, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        url = admin_url.set(database=database_name).render_as_string(hide_password=False)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "DATABASE_URL": url},
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            yield url
        finally:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


@pytest.fixture
def setup(database_url):
    async def populate():
        database = Database(database_url)
        try:
            fixtures = []
            for _ in range(2):
                async with database.sessions() as session:
                    fixtures.append(await seed_demo(session, uuid4()))
            return fixtures
        finally:
            await database.close()

    first, second = asyncio.run(populate())
    settings = Settings(_env_file=None, database_url=database_url, operator_api_key="test-operator")
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client, first, second


def post(client, fixture, key="assignment-1", body=None):
    return client.post(
        f"/v1/firms/{fixture['firm_id']}/tasks/medical-records",
        headers={**AUTH, "Idempotency-Key": key},
        json=body if body is not None else fixture["assignment"],
    )


def connect(database_url):
    return psycopg.connect(database_url.replace("postgresql+psycopg://", "postgresql://"))


def test_creation_persists_domain_request_event_and_dispatch(setup, database_url):
    client, fixture, _ = setup
    response = post(client, fixture)
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "PENDING"
    assert task["agent_key"] == "medical_records_collector"
    assert task["records_request"]["status"] == "DRAFT"
    assert len(task["records_request"]["requirements"]) == 2
    assert client.get(response.headers["Location"], headers=AUTH).json() == task
    events = client.get(response.headers["Location"] + "/events", headers=AUTH).json()
    assert [event["event_type"] for event in events] == ["task.created"]
    with connect(database_url) as connection:
        row = connection.execute(
            "SELECT destination, destination_key, status FROM outbox_messages WHERE tenant_id = %s",
            (UUID(fixture["firm_id"]),),
        ).fetchone()
        assert row == ("temporal.start_task", task["workflow_id"], "PENDING")


def test_replay_and_conflicting_reuse(setup, database_url):
    client, fixture, _ = setup
    first = post(client, fixture)
    second = post(client, fixture)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.headers["Idempotency-Replayed"] == "true"
    changed = {**fixture["assignment"], "objective": "Different assignment"}
    assert post(client, fixture, body=changed).status_code == 409
    with connect(database_url) as connection:
        for table in ("records_requests", "activity_events", "outbox_messages"):
            count = connection.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(
                    sql.Identifier(table)
                ),
                (UUID(fixture["firm_id"]),),
            ).fetchone()[0]
            assert count == 1


def test_concurrent_duplicate_creation_has_one_winner(setup):
    client, fixture, _ = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client, fixture), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]


@pytest.mark.parametrize(
    "field", ["case_id", "parent_task_id", "patient_contact_id", "provider_contact_id"]
)
def test_rejects_other_firm_references(setup, field):
    client, first, second = setup
    body = {**first["assignment"], field: second["assignment"][field]}
    response = post(client, first, body=body)
    assert response.status_code == 404, response.text


def test_task_read_and_history_are_tenant_scoped(setup):
    client, first, second = setup
    task = post(client, first).json()
    other_firm_path = f"/v1/firms/{second['firm_id']}/tasks/{task['id']}"
    assert client.get(other_firm_path, headers=AUTH).status_code == 404
    assert client.get(other_firm_path + "/events", headers=AUTH).status_code == 404
    assert client.get(other_firm_path + "/executions", headers=AUTH).status_code == 404


def test_case_roles_are_required(setup, database_url):
    client, fixture, _ = setup
    with connect(database_url) as connection:
        connection.execute(
            "UPDATE case_contacts SET roles = ARRAY['WITNESS'] "
            "WHERE tenant_id = %s AND contact_id = %s",
            (UUID(fixture["firm_id"]), UUID(fixture["assignment"]["provider_contact_id"])),
        )
    assert post(client, fixture).status_code == 422


def test_creation_rolls_back_if_outbox_write_fails(setup, database_url, monkeypatch):
    client, fixture, _ = setup
    execute = AsyncSession.execute

    async def fail_outbox(self, statement, *args, **kwargs):
        if getattr(getattr(statement, "table", None), "name", None) == "outbox_messages":
            raise RuntimeError("simulated dispatch-record failure")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_outbox)
    assert post(client, fixture).status_code == 500
    with connect(database_url) as connection:
        for table in (
            "records_requests",
            "record_requirements",
            "activity_events",
            "outbox_messages",
        ):
            assert (
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {} WHERE tenant_id = %s").format(
                        sql.Identifier(table)
                    ),
                    (UUID(fixture["firm_id"]),),
                ).fetchone()[0]
                == 0
            )
        assert (
            connection.execute(
                "SELECT count(*) FROM tasks WHERE tenant_id = %s AND kind = 'AGENT'",
                (UUID(fixture["firm_id"]),),
            ).fetchone()[0]
            == 0
        )


def test_database_rejects_cross_firm_patient_even_without_api(setup, database_url):
    client, first, second = setup
    task = post(client, first).json()
    with connect(database_url) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "UPDATE records_requests SET patient_contact_id = %s WHERE task_id = %s",
                (UUID(second["assignment"]["patient_contact_id"]), UUID(task["id"])),
            )


def test_no_credential_no_writes_and_no_permission_fields_in_body(setup):
    client, fixture, _ = setup
    path = f"/v1/firms/{fixture['firm_id']}/tasks/medical-records"
    assert client.post(path, json=fixture["assignment"]).status_code in (401, 403)
    body = {**fixture["assignment"], "tenant_id": str(uuid4())}
    assert post(client, fixture, body=body).status_code == 422


def test_invalid_scope_and_unpublished_agent_are_rejected(setup):
    client, fixture, _ = setup
    body = {**fixture["assignment"], "agent_version": 99}
    assert post(client, fixture, body=body).status_code == 422
    body = {
        **fixture["assignment"],
        "requirements": [{"record_type": "RECORDS", "all_dates": False}],
    }
    assert post(client, fixture, body=body).status_code == 422


def test_postgres_migrations_match_models(database_url):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def wait_for_status(database, task_id, statuses, timeout=30):
    from sqlalchemy import select

    from agents_platform.models import Task

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with database.sessions() as session:
            task = await session.scalar(select(Task).where(Task.id == UUID(task_id)))
            if task.status in statuses:
                return task.status
        await asyncio.sleep(0.1)
    pytest.fail(f"Task did not reach {statuses}")


def task_worker(database, settings, client, queue):
    from temporalio.worker import Worker

    from agents_platform.execution import TaskActivities
    from agents_platform.orchestration.workflows import TaskWorkflow

    activities = TaskActivities(database, settings)
    return Worker(
        client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[activities.execute_task, activities.fail_task],
    )


async def test_wait_restart_provider_response_and_completion(setup, database_url):
    from sqlalchemy import func, select
    from temporalio.client import Client

    from agents_platform.dispatch import dispatch_once
    from agents_platform.models import Execution, TaskInbox, ToolInvocation

    api, fixture, _ = setup
    task = post(api, fixture).json()
    settings = Settings(
        _env_file=None, database_url=database_url, agent_mode="scripted", demo_follow_up_seconds=60
    )
    database = Database(database_url)
    client = await Client.connect("localhost:7233")
    queue = f"test-{uuid4()}"
    try:
        async with task_worker(database, settings, client, queue):
            await dispatch_once(database, client, queue, UUID(fixture["firm_id"]))
            await wait_for_status(database, task["id"], {"WAITING", "FAILED"})
            async with database.sessions() as session:
                first = await session.scalar(
                    select(Execution).where(Execution.task_id == UUID(task["id"]))
                )
                assert first.status == "SUCCEEDED", first.error_type
                assert first.context_json["task"]["records_request"]["status"] == "DRAFT"
                assert {i["scope"] for i in first.context_json["instructions"]} == {
                    "FIRM",
                    "PROVIDER",
                    "FIRM_PROVIDER",
                }
        # No worker is running when the provider response is saved.
        path = f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates"
        headers = {**AUTH, "Idempotency-Key": "records-ready"}
        body = {
            "status": "RECEIVED",
            "message": "Simulated complete records and imaging are ready.",
        }
        response = api.post(path, headers=headers, json=body)
        assert response.status_code == 201, response.text
        assert api.post(path, headers=headers, json=body).status_code == 200
        async with task_worker(database, settings, client, queue):
            await dispatch_once(database, client, queue, UUID(fixture["firm_id"]))
            result = await asyncio.wait_for(
                client.get_workflow_handle(task["workflow_id"]).result(), 30
            )
            assert result["status"] == "COMPLETED"
        async with database.sessions() as session:
            runs = (
                await session.scalars(
                    select(Execution)
                    .where(Execution.task_id == UUID(task["id"]))
                    .order_by(Execution.cycle)
                )
            ).all()
            assert len(runs) == 2
            assert runs[-1].context_json["task"]["records_request"]["status"] == "RECEIVED"
            assert runs[-1].context_json["task"]["progress_json"]["last_summary"]
            assert runs[-1].context_json["sources"][0]["content"]["message"] == body["message"]
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
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TaskInbox)
                    .where(
                        TaskInbox.task_id == UUID(task["id"]),
                        TaskInbox.consumed_by_execution_id.is_(None),
                    )
                )
                == 0
            )
        history = api.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/executions", headers=AUTH
        )
        assert history.status_code == 200 and len(history.json()) == 2
        assert history.json()[-1]["mode"] == "scripted"
    finally:
        await database.close()


async def test_timer_creates_new_activation_without_duplicate_send(setup, database_url):
    from sqlalchemy import func, select
    from temporalio.client import Client

    from agents_platform.dispatch import dispatch_once
    from agents_platform.models import Execution, ToolInvocation

    api, fixture, _ = setup
    task = post(api, fixture).json()
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        agent_mode="scripted",
        demo_follow_up_seconds=1,
        demo_max_activations=2,
    )
    database = Database(database_url)
    client = await Client.connect("localhost:7233")
    queue = f"timer-test-{uuid4()}"
    try:
        async with task_worker(database, settings, client, queue):
            await dispatch_once(database, client, queue, UUID(fixture["firm_id"]))
            assert await wait_for_status(database, task["id"], {"BLOCKED", "FAILED"}) == "BLOCKED"
            async with database.sessions() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(Execution)
                        .where(Execution.task_id == UUID(task["id"]))
                    )
                    == 2
                )
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
            # Close this otherwise-waiting test workflow via the supported provider event.
            path = f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates"
            assert (
                api.post(
                    path,
                    headers={**AUTH, "Idempotency-Key": "finish"},
                    json={"status": "RECEIVED", "message": "Simulated records ready"},
                ).status_code
                == 201
            )
            await dispatch_once(database, client, queue, UUID(fixture["firm_id"]))
            result = await asyncio.wait_for(
                client.get_workflow_handle(task["workflow_id"]).result(), 30
            )
            assert result["status"] == "COMPLETED"
    finally:
        await database.close()


async def test_new_provider_event_invalidates_inflight_decision(setup, database_url, monkeypatch):
    from types import SimpleNamespace

    from agents import Runner, Usage
    from sqlalchemy import select

    from agents_platform.execution import TaskActivities
    from agents_platform.models import Execution, TaskInbox
    from agents_platform.task_contracts import NextAction, ProviderUpdate, RecordsDecision
    from agents_platform.tasks import receive_provider_update

    api, fixture, _ = setup
    task = post(api, fixture).json()
    database = Database(database_url)
    activities = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    activation = {
        "tenant_id": fixture["firm_id"],
        "task_id": task["id"],
        "cycle": 0,
        "trigger": "created",
    }

    async def interrupted_run(*args, **kwargs):
        async with database.sessions() as session:
            await receive_provider_update(
                session,
                UUID(fixture["firm_id"]),
                UUID(task["id"]),
                ProviderUpdate(status="RECEIVED", message="Arrived during model call"),
                "during-run",
            )
        return SimpleNamespace(
            final_output=RecordsDecision(
                summary="Outdated pending result",
                observations=[],
                received_source_ids=[],
                next_action=NextAction(type="wait", follow_up_seconds=30, reason="outdated"),
            ),
            context_wrapper=SimpleNamespace(usage=Usage()),
        )

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Runner, "run", interrupted_run)
            assert (await activities.execute_task(activation))["status"] == "CONTINUE"
        async with database.sessions() as session:
            previous = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]))
            )
            assert previous.status == "STALE"
            inbox = await session.scalar(
                select(TaskInbox).where(TaskInbox.task_id == UUID(task["id"]))
            )
            assert inbox.consumed_by_execution_id is None
        outcome = await activities.execute_task({**activation, "cycle": 1, "trigger": "event"})
        assert outcome["status"] == "COMPLETED"
        # A retry after commit returns exactly the saved outcome, with no new tools.
        assert (
            await activities.execute_task({**activation, "cycle": 1, "trigger": "event"}) == outcome
        )
    finally:
        await database.close()


async def test_model_cannot_complete_without_saved_record_evidence(
    setup, database_url, monkeypatch
):
    from types import SimpleNamespace

    from agents import Runner, Usage

    from agents_platform.execution import TaskActivities
    from agents_platform.task_contracts import NextAction, RecordsDecision

    api, fixture, _ = setup
    task = post(api, fixture).json()
    database = Database(database_url)
    activities = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))

    async def invented_completion(*args, **kwargs):
        return SimpleNamespace(
            final_output=RecordsDecision(
                summary="Unsupported completion",
                observations=[],
                received_source_ids=[],
                next_action=NextAction(
                    type="complete", follow_up_seconds=None, reason="no evidence"
                ),
            ),
            context_wrapper=SimpleNamespace(usage=Usage()),
        )

    monkeypatch.setattr(Runner, "run", invented_completion)
    try:
        outcome = await activities.execute_task(
            {
                "tenant_id": fixture["firm_id"],
                "task_id": task["id"],
                "cycle": 0,
                "trigger": "created",
            }
        )
        assert outcome["status"] == "CONTINUE"
        assert "saved evidence" in outcome["reason"]
        response = api.get(f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}", headers=AUTH)
        assert response.json()["status"] != "COMPLETED"
    finally:
        await database.close()


def test_dashboard_lists_only_selected_firm_and_paginates(setup):
    client, first, second = setup
    a = post(client, first, key="dashboard-a").json()
    b = post(client, first, key="dashboard-b").json()
    other = post(client, second, key="dashboard-other").json()
    base = f"/v1/operator/firms/{first['firm_id']}"
    assert client.get(base + "/overview").status_code in (401, 403)
    overview = client.get(base + "/overview", headers=AUTH).json()
    assert overview["counts"] == {"PENDING": 2}
    assert {c["case_id"] for c in overview["contacts"]} == {first["assignment"]["case_id"]}
    page = client.get(base + "/tasks?limit=1", headers=AUTH).json()
    next_page = client.get(
        base + f"/tasks?limit=1&before={page['next_cursor']}", headers=AUTH
    ).json()
    assert {page["items"][0]["id"], next_page["items"][0]["id"]} == {a["id"], b["id"]}
    assert next_page["next_cursor"] is None
    assert client.get(base + f"/tasks?before={other['id']}", headers=AUTH).status_code == 404


def test_comment_mentions_are_audited_and_wake_is_transactional(setup, database_url):
    client, first, second = setup
    task = post(client, first).json()
    base = f"/v1/operator/firms/{first['firm_id']}"
    path = base + f"/tasks/{task['id']}/comments"
    headers = {**AUTH, "Idempotency-Key": "operator-followup"}
    body = {
        "text": "Please check the saved provider update.",
        "mentions": ["medical_records_collector", "supervisor"],
        "wake_agent": True,
    }
    assert client.post(path, headers=headers, json=body).status_code == 201
    replay = client.post(path, headers=headers, json=body)
    assert replay.json()["replayed"] is True
    assert client.post(path, headers=headers, json={**body, "text": "Changed"}).status_code == 409
    assert (
        client.post(
            path.replace(first["firm_id"], second["firm_id"]), headers=headers, json=body
        ).status_code
        == 404
    )
    events = client.get(base + f"/activity?task_id={task['id']}", headers=AUTH).json()["items"]
    comment = next(e for e in events if e["type"] == "task.comment_added")
    assert comment["payload"]["mentions"] == body["mentions"]
    with connect(database_url) as connection:
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM task_inbox WHERE task_id=%s), "
            "(SELECT count(*) FROM outbox_messages WHERE event_id=%s), "
            "(SELECT count(*) FROM sources WHERE id=%s)",
            (UUID(task["id"]), UUID(comment["id"]), UUID(comment["payload"]["source_id"])),
        ).fetchone()
        assert counts == (1, 1, 1)
        assert (
            connection.execute(
                "SELECT version FROM tasks WHERE id=%s", (UUID(task["id"]),)
            ).fetchone()[0]
            == 2
        )


def test_supervisor_mentions_do_not_run_unconnected_agents(setup, database_url):
    client, first, _ = setup
    task = post(client, first).json()
    base = f"/v1/operator/firms/{first['firm_id']}"
    path = base + f"/tasks/{task['id']}/comments"
    body = {"text": "@supervisor review this", "mentions": ["supervisor"], "wake_agent": True}
    assert (
        client.post(path, headers={**AUTH, "Idempotency-Key": "bad-wake"}, json=body).status_code
        == 409
    )
    body["wake_agent"] = False
    saved = client.post(path, headers={**AUTH, "Idempotency-Key": "note"}, json=body)
    assert saved.status_code == 201
    assert saved.json()["woke_agent"] is False
    with connect(database_url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM task_inbox WHERE task_id=%s", (UUID(task["id"]),)
            ).fetchone()[0]
            == 0
        )
        connection.execute("UPDATE tasks SET status='COMPLETED' WHERE id=%s", (UUID(task["id"]),))
    body.update(mentions=["medical_records_collector"], wake_agent=True)
    assert (
        client.post(path, headers={**AUTH, "Idempotency-Key": "closed"}, json=body).status_code
        == 409
    )
    body["wake_agent"] = False
    assert (
        client.post(path, headers={**AUTH, "Idempotency-Key": "closed-note"}, json=body).status_code
        == 201
    )


def test_dashboard_activity_pagination_and_firm_scope(setup):
    client, first, second = setup
    task = post(client, first).json()
    base = f"/v1/operator/firms/{first['firm_id']}"
    path = base + f"/tasks/{task['id']}/comments"
    for i in range(3):
        response = client.post(
            path, headers={**AUTH, "Idempotency-Key": str(i)}, json={"text": f"Audit note {i}"}
        )
        assert response.status_code == 201
    events, cursor = [], None
    for _ in range(4):
        page = client.get(
            base
            + f"/activity?limit=1&task_id={task['id']}"
            + (f"&before={cursor}" if cursor else ""),
            headers=AUTH,
        ).json()
        events.extend(page["items"])
        cursor = page["next_cursor"]
    assert len({e["id"] for e in events}) == 4
    assert cursor is None
    other_base = base.replace(first["firm_id"], second["firm_id"])
    assert (
        client.get(other_base + f"/activity?task_id={task['id']}", headers=AUTH).json()["items"]
        == []
    )
    assert (
        client.get(other_base + f"/activity?before={events[0]['id']}", headers=AUTH).status_code
        == 404
    )


async def test_operator_comment_is_in_next_activation_context(setup, database_url):
    from sqlalchemy import select

    from agents_platform.execution import TaskActivities
    from agents_platform.models import Execution, TaskInbox

    client, fixture, _ = setup
    task = post(client, fixture).json()
    database = Database(database_url)
    activities = TaskActivities(database, Settings(_env_file=None, agent_mode="scripted"))
    activation = {
        "tenant_id": fixture["firm_id"],
        "task_id": task["id"],
        "cycle": 0,
        "trigger": "created",
    }
    try:
        assert (await activities.execute_task(activation))["status"] == "WAITING"
        path = f"/v1/operator/firms/{fixture['firm_id']}/tasks/{task['id']}/comments"
        message = "Provider asked us to check again tomorrow."
        response = client.post(
            path,
            headers={**AUTH, "Idempotency-Key": "guidance"},
            json={"text": message, "mentions": ["medical_records_collector"], "wake_agent": True},
        )
        assert response.status_code == 201
        assert (await activities.execute_task({**activation, "cycle": 1, "trigger": "event"}))[
            "status"
        ] == "WAITING"
        async with database.sessions() as session:
            run = await session.scalar(
                select(Execution).where(Execution.task_id == UUID(task["id"]), Execution.cycle == 1)
            )
            assert any(s["content"].get("text") == message for s in run.context_json["sources"])
            entry = await session.scalar(
                select(TaskInbox).where(TaskInbox.task_id == UUID(task["id"]))
            )
            assert entry.consumed_by_execution_id == run.id
    finally:
        await database.close()

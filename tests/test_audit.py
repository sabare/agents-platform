"""Journal contract, real rollback, permissions, concurrency, retention and tampering."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.audit import (
    AuditEvent,
    append_event,
    audit_scope,
    authorize_tool,
    invoke_tool,
    reject,
)
from agents_platform.audit_integrity import verify
from agents_platform.audit_roles import provision
from agents_platform.config import Settings
from agents_platform.db import Database
from agents_platform.execution import TaskActivities

integration = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Needs PostgreSQL"
)


def test_contract_rejects_unknown_fields_and_outcomes():
    with pytest.raises(ValidationError):
        AuditEvent(event_type="tool.attempt", action="send", outcome="MAGIC", reason_code="TEST")
    with pytest.raises(ValidationError):
        AuditEvent(
            event_type="tool.attempt",
            action="send",
            outcome="FAILED",
            reason_code="TEST",
            raw_prompt="private",
        )


def read_events(url, tenant):
    with connect(url) as connection:
        return [
            r[0]
            for r in connection.execute(
                "SELECT event FROM audit.events WHERE stream_id=%s ORDER BY sequence",
                (UUID(str(tenant)),),
            )
        ]


@integration
async def test_failed_and_denied_tools_survive_rollback_without_sensitive_text(database_url):
    database = Database(database_url)
    tenant = uuid4()
    try:
        with audit_scope(database, tenant_id=tenant):

            async def failed(context, raw):
                async with database.sessions.begin() as session:
                    await authorize_tool(session, policy_version="test.v1")
                    raise RuntimeError("private medical report and secret token")

            with pytest.raises(RuntimeError):
                await invoke_tool("send", failed, None, '{"text":"private argument"}')

            async def denied(context, raw):
                async with database.sessions.begin():
                    reject("CONTACT_HOLD")
                    raise ValueError("private reason")

            with pytest.raises(ValueError):
                await invoke_tool("send", denied, None, "{}")
        events = read_events(database_url, tenant)
        assert [e["outcome"] for e in events] == [
            "ATTEMPTED",
            "ALLOWED",
            "FAILED",
            "ATTEMPTED",
            "DENIED",
            "REJECTED",
        ]
        assert events[1]["policy_version"] == "test.v1"
        assert events[4]["reason_code"] == "CONTACT_HOLD"
        assert len({e["attempt_id"] for e in events}) == 2
        assert "private" not in json.dumps(events) and "secret token" not in json.dumps(events)
    finally:
        await database.close()


@integration
async def test_audit_failure_prevents_tool_callback(database_url, monkeypatch):
    from agents_platform import audit

    database = Database(database_url)
    called = False

    async def unavailable(*args):
        raise RuntimeError("audit unavailable")

    async def callback(context, raw):
        nonlocal called
        called = True

    monkeypatch.setattr(audit, "persist", unavailable)
    try:
        with audit_scope(database, tenant_id=uuid4()):
            with pytest.raises(RuntimeError):
                await invoke_tool("send", callback, None, "{}")
        assert not called
    finally:
        await database.close()


@integration
async def test_execution_captures_authorization_committed_effect_and_failed_http(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    database = Database(database_url)
    try:
        activities = TaskActivities(database, Settings(_env_file=None, database_url=database_url))
        result = await activities.execute_task(
            {"tenant_id": fixture["firm_id"], "task_id": task["id"], "cycle": 0, "trigger": "start"}
        )
        assert result["status"] == "WAITING"
        denied = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}",
            headers={"Authorization": "Bearer invalid"},
        )
        assert denied.status_code == 401
        conflict = post(
            client, fixture, body={**fixture["assignment"], "objective": "Other objective"}
        )
        assert conflict.status_code == 409
        response = client.get(
            f"/v1/firms/{fixture['firm_id']}/audit/events",
            headers=AUTH,
            params={"task_id": task["id"], "limit": 500},
        )
        assert response.status_code == 200, response.text
        filtered = [r["event"] for r in response.json()["events"]]
        assert filtered and all(e["task_id"] == task["id"] for e in filtered)
        entries = read_events(database_url, fixture["firm_id"])
        assert any(
            e["event_type"] == "external_effect.result"
            and e["action"] == "send_records_request"
            and e["details"]["simulated"]
            for e in entries
        )
        assert any(
            e["event_type"] == "authorization.decision"
            and e["policy_version"] == "agent-tool.v1"
            and e["outcome"] == "ALLOWED"
            for e in entries
        )
        assert any(
            e["event_type"] == "authorization.decision" and e["outcome"] == "DENIED"
            for e in entries
        )
        assert any(
            e["event_type"] == "action.result" and e["details"].get("status_code") == 409
            for e in entries
        )
        assert any(e["event_type"] == "tool.committed" for e in entries)
        with connect(database_url) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            checkpoint, _ = verify(connection, UUID(fixture["firm_id"]))
            assert checkpoint["sequence"] > 5
    finally:
        await database.close()


@integration
async def test_business_and_audit_commit_or_rollback_together(database_url):
    database = Database(database_url)
    tenant = uuid4()
    try:
        with pytest.raises(RuntimeError):
            async with database.sessions.begin() as session:
                await append_event(
                    session,
                    AuditEvent(
                        tenant_id=tenant,
                        event_type="business.event",
                        action="test",
                        outcome="SUCCEEDED",
                        reason_code="TEST",
                    ),
                )
                raise RuntimeError("business rollback")
        assert read_events(database_url, tenant) == []
    finally:
        await database.close()


@integration
def test_concurrent_append_has_no_forks_and_external_checkpoint_detects_rewrite(database_url):
    tenant = uuid4()

    def write(index):
        with connect(database_url) as connection:
            event = AuditEvent(
                tenant_id=tenant,
                event_type="tool.attempt",
                action="test",
                outcome="ATTEMPTED",
                reason_code="TEST",
            )
            connection.execute("SELECT audit.append_event(%s::jsonb)", (event.model_dump_json(),))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(20)))
    with connect(database_url) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        checkpoint, rows = verify(connection, tenant)
        assert checkpoint["sequence"] == 20 and len(rows) == 20
    # An administrator can bypass protections; verification must reveal it.
    with connect(database_url) as connection:
        connection.execute("SET LOCAL ROLE agents_audit_owner")
        connection.execute(
            'UPDATE audit.events SET event=event || \'{"reason_code":"TAMPERED"}\'::jsonb WHERE stream_id=%s AND sequence=2',  # noqa: E501 -- SQL fixture
            (tenant,),
        )
        with pytest.raises(ValueError, match="integrity failure"):
            verify(connection, tenant, checkpoint)
        connection.rollback()
    with connect(database_url) as connection:
        connection.execute("SET LOCAL ROLE agents_audit_owner")
        connection.execute("DELETE FROM audit.events WHERE stream_id=%s AND sequence=20", (tenant,))
        with pytest.raises(ValueError, match="head mismatch"):
            verify(connection, tenant)
        connection.rollback()
    with connect(database_url) as connection:
        wrong = {**checkpoint, "hash": "f" * 64}
        with pytest.raises(ValueError, match="External checkpoint"):
            verify(connection, tenant, wrong)


@integration
def test_runtime_role_cannot_rewrite_or_prune_audit_and_can_run_app(setup, database_url):
    client, fixture, _ = setup
    role, password = "audit_test_" + uuid4().hex[:12], uuid4().hex
    with connect(database_url) as connection:
        provision(connection, role, password)
    url = make_url(database_url).set(username=role, password=password, drivername="postgresql")
    try:
        with psycopg.connect(
            url.render_as_string(hide_password=False), autocommit=True
        ) as connection:
            assert connection.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            for statement in [
                "UPDATE audit.events SET event='{}'",
                "DELETE FROM audit.events",
                "TRUNCATE audit.events",
                "INSERT INTO audit.events VALUES (NULL,NULL,NULL,NULL,NULL,NULL)",
                "SET ROLE agents_audit_owner",
                "SELECT audit.prune(gen_random_uuid(),1,'fake')",
                "ALTER TABLE audit.events DISABLE TRIGGER ALL",
            ]:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(statement)
            event = AuditEvent(
                tenant_id=UUID(fixture["firm_id"]),
                event_type="tool.attempt",
                action="test",
                outcome="ATTEMPTED",
                reason_code="TEST",
            )
            connection.execute("SELECT audit.append_event(%s::jsonb)", (event.model_dump_json(),))
        # Run the API against real restricted credentials, including database triggers.
        from fastapi.testclient import TestClient

        from agents_platform.api import create_app

        with TestClient(
            create_app(
                Settings(
                    _env_file=None,
                    database_url=url.set(drivername="postgresql+psycopg").render_as_string(
                        hide_password=False
                    ),
                    operator_api_key="test-operator",
                )
            ),
            raise_server_exceptions=False,
        ) as restricted:
            response = post(restricted, fixture, key="restricted-login")
            assert response.status_code == 201, response.text

            async def execute_restricted():
                runtime = Database(
                    url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
                )
                try:
                    return await TaskActivities(runtime, Settings(_env_file=None)).execute_task(
                        {
                            "tenant_id": fixture["firm_id"],
                            "task_id": response.json()["id"],
                            "cycle": 0,
                            "trigger": "start",
                        }
                    )
                finally:
                    await runtime.close()

            assert asyncio.run(execute_restricted())["status"] == "WAITING"
            audit = restricted.get(f"/v1/firms/{fixture['firm_id']}/audit/events", headers=AUTH)
            assert audit.status_code == 200, audit.text
    finally:
        with connect(database_url) as connection:
            connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@integration
def test_retention_and_legal_hold_and_archived_prefix_anchor(database_url):
    tenant = uuid4()
    event = AuditEvent(
        tenant_id=tenant,
        event_type="tool.attempt",
        action="test",
        outcome="ATTEMPTED",
        reason_code="TEST",
    )
    with connect(database_url) as connection:
        connection.execute("SELECT audit.append_event(%s::jsonb)", (event.model_dump_json(),))
    with connect(database_url) as connection:
        checkpoint, _ = verify(connection, tenant)
        with pytest.raises(psycopg.errors.RaiseException, match="retention period"):
            connection.execute("SELECT audit.prune(%s,1,%s)", (tenant, checkpoint["hash"]))
        connection.rollback()
        # Simulate passage of time in this disposable stream using owner access.
        connection.execute("SET LOCAL ROLE agents_audit_owner")
        connection.execute(
            "UPDATE audit.events SET event=event || jsonb_build_object('retained_until',clock_timestamp()-interval '1 day') WHERE stream_id=%s",  # noqa: E501 -- SQL fixture
            (tenant,),
        )
        connection.execute(
            "UPDATE audit.events SET event_hash=encode(public.digest(convert_to(previous_hash || E'\\n' || event::text,'UTF8'),'sha256'),'hex') WHERE stream_id=%s",  # noqa: E501 -- SQL fixture
            (tenant,),
        )
        connection.execute(
            "UPDATE audit.heads SET last_hash=(SELECT event_hash FROM audit.events WHERE stream_id=%s) WHERE stream_id=%s",  # noqa: E501 -- SQL fixture
            (tenant, tenant),
        )
        connection.commit()
        checkpoint, _ = verify(connection, tenant)
        connection.execute("SELECT audit.set_retention(%s,365,true)", (tenant,))
        connection.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="legal hold"):
            connection.execute("SELECT audit.prune(%s,1,%s)", (tenant, checkpoint["hash"]))
        connection.rollback()
        connection.execute("SELECT audit.set_retention(%s,365,false)", (tenant,))
        connection.commit()
        removed = connection.execute(
            "SELECT audit.prune(%s,1,%s)", (tenant, checkpoint["hash"])
        ).fetchone()[0]
        connection.commit()
        assert removed == 1
        current, _ = verify(connection, tenant, checkpoint)
        assert current["anchor_sequence"] == 1 and current["anchor_hash"] == checkpoint["hash"]

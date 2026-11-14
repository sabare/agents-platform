"""Supervisor intake uses real persistence and domain creation, with a controlled model boundary."""

import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_task_persistence import AUTH, connect
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform import assignment_router as routing
from agents_platform.models import CaseCoordination, Task

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Requires PostgreSQL"),
]


def base(f):
    return f"/v1/firms/{f['firm_id']}/cases/{f['assignment']['case_id']}/assignments"


def records(f):
    a = f["assignment"]
    return {
        "kind": "records",
        "targets": [
            {k: a[k] for k in ("patient_contact_id", "provider_contact_id", "requirements")}
        ],
    }


def checkin(f):
    return {
        "kind": "check_in",
        "assignment": {
            "case_id": f["assignment"]["case_id"],
            "client_contact_id": f["assignment"]["patient_contact_id"],
            "scheduled_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def monthly(f):
    return {
        "kind": "monthly",
        "assignment": {
            "client_contact_id": f["assignment"]["patient_contact_id"],
            "timezone": "Asia/Kolkata",
            "day_of_month": 5,
            "local_time": "09:00",
            "max_occurrences": 3,
        },
    }


def mock_plan(monkeypatch, steps=None, status="ready"):
    async def interpret(settings, context):
        return routing.AssignmentPlan(
            status=status,
            title="Collect hospital information",
            summary="Proposed work",
            steps=steps or [],
            questions=[] if status == "ready" else ["Which hospital should we contact?"],
        ), {"requests": 1}

    monkeypatch.setattr(routing, "interpret_description", interpret)


def propose(client, f):
    return client.post(
        base(f) + "/plan",
        headers=AUTH,
        json={"description": "Collect records and check in with the client"},
    )


def approve(client, f, proposal):
    return client.post(base(f) + f"/{proposal['plan_id']}/approve", headers=AUTH)


def count(url, table, f):
    with connect(url) as c:
        return c.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id=%s"
            + (" AND task_type != 'records.group'" if table == "tasks" else ""),
            (UUID(f["firm_id"]),),
        ).fetchone()[0]


def test_planning_is_authenticated_read_only_and_needs_approval(setup, database_url, monkeypatch):
    client, f, _ = setup
    mock_plan(monkeypatch, [records(f)])
    assert (
        client.post(base(f) + "/plan", json={"description": "collect records"}).status_code == 401
    )
    draft = propose(client, f)
    assert draft.status_code == 200, draft.text
    assert count(database_url, "tasks", f) == 0
    assert count(database_url, "outbox_messages", f) == 0
    result = approve(client, f, draft.json())
    assert result.status_code == 200, result.text
    assert result.json()["tasks"][0]["kind"] == "records"
    assert count(database_url, "outbox_messages", f) > 0
    replay = approve(client, f, draft.json())
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["tasks"] == result.json()["tasks"]
    assert count(database_url, "tasks", f) == 1  # Group; specialists are delegated durably.


@pytest.mark.parametrize("status", ["needs_clarification", "unsupported"])
def test_questions_cannot_start_work(setup, database_url, monkeypatch, status):
    client, f, _ = setup
    mock_plan(monkeypatch, status=status)
    draft = propose(client, f)
    assert draft.status_code == 200, draft.text
    assert approve(client, f, draft.json()).status_code == 409
    assert count(database_url, "tasks", f) == 0


def test_foreign_participants_and_case_are_rejected(setup, monkeypatch):
    client, f, other = setup
    mock_plan(monkeypatch, [records(other)])
    assert propose(client, f).status_code == 422
    step = checkin(f)
    step["assignment"]["case_id"] = other["assignment"]["case_id"]
    mock_plan(monkeypatch, [step])
    assert propose(client, f).status_code == 422


@pytest.mark.parametrize("kind", ["check_in", "monthly", "series"])
def test_client_capabilities_use_existing_durable_intake(setup, database_url, monkeypatch, kind):
    client, f, _ = setup
    step = checkin(f) if kind == "check_in" else monthly(f)
    if kind == "series":
        step = {
            "kind": "series",
            "assignment": {
                "client_contact_id": f["assignment"]["patient_contact_id"],
                "first_due_at": datetime.now(timezone.utc).isoformat(),
                "interval_seconds": 604800,
                "max_occurrences": 4,
            },
        }
    mock_plan(monkeypatch, [step])
    draft = propose(client, f)
    assert draft.status_code == 200, draft.text
    result = approve(client, f, draft.json())
    assert result.status_code == 200, result.text
    assert result.json()["tasks"][0]["kind"] == kind
    assert count(database_url, "outbox_messages", f) > 0


def test_combined_plan_is_atomic_and_replay_safe(setup, database_url, monkeypatch):
    client, f, _ = setup
    # Deliberately put check-in first: intake must establish records coordination first.
    mock_plan(monkeypatch, [checkin(f), records(f)])
    draft = propose(client, f).json()
    result = approve(client, f, draft)
    assert result.status_code == 200, result.text
    assert len(result.json()["tasks"]) == 2
    before = count(database_url, "tasks", f)
    assert approve(client, f, draft).json()["replayed"]
    assert count(database_url, "tasks", f) == before


def test_failed_step_rolls_back_entire_plan(setup, database_url, monkeypatch):
    client, f, _ = setup
    # Two cadences for the same client cannot both be created.
    conflicting = monthly(f)
    conflicting["assignment"]["day_of_month"] = 12
    mock_plan(monkeypatch, [records(f), monthly(f), conflicting])
    draft = propose(client, f).json()
    result = approve(client, f, draft)
    assert result.status_code == 409, result.text
    assert count(database_url, "tasks", f) == 0
    assert count(database_url, "outbox_messages", f) == 0


def test_scope_change_after_planning_blocks_approval(setup, database_url, monkeypatch):
    client, f, _ = setup
    mock_plan(monkeypatch, [records(f)])
    draft = propose(client, f).json()
    with connect(database_url) as c:
        c.execute(
            "UPDATE case_contacts SET roles=ARRAY['CLIENT']::varchar[] "
            "WHERE tenant_id=%s AND contact_id=%s",
            (UUID(f["firm_id"]), UUID(f["assignment"]["provider_contact_id"])),
        )
    assert approve(client, f, draft).status_code == 422
    assert count(database_url, "tasks", f) == 0


def test_existing_objective_revision_requires_replanning(setup, database_url, monkeypatch):
    client, f, _ = setup
    mock_plan(monkeypatch, [records(f)])
    first = propose(client, f).json()
    stale = propose(client, f).json()
    assert approve(client, f, first).status_code == 200
    assert approve(client, f, stale).status_code == 409
    assert count(database_url, "tasks", f) == 1


def test_cannot_approve_another_cases_plan(setup, monkeypatch):
    client, f, other = setup
    mock_plan(monkeypatch, [records(f)])
    draft = propose(client, f).json()
    assert approve(client, other, draft).status_code == 404


async def test_multi_hospital_plan_delegates_separate_specialists(setup, database_url, monkeypatch):
    from test_memory import runtime
    from test_supervisor import activation

    from agents_platform.coordination import SupervisorActivities

    client, f, _ = setup
    provider = uuid4()
    with connect(database_url) as c:
        c.execute(
            "INSERT INTO contacts(id,tenant_id,display_name,kind) "
            "VALUES(%s,%s,'Second Hospital','ORGANIZATION')",
            (provider, UUID(f["firm_id"])),
        )
        c.execute(
            "INSERT INTO case_contacts(id,tenant_id,case_id,contact_id,roles) "
            "VALUES(%s,%s,%s,%s,ARRAY['MEDICAL_PROVIDER']::varchar[])",
            (uuid4(), UUID(f["firm_id"]), UUID(f["assignment"]["case_id"]), provider),
        )
    step = records(f)
    step["targets"][0]["task_title"] = "Collect records from the first hospital"
    step["targets"].append(
        {
            **step["targets"][0],
            "provider_contact_id": str(provider),
            "task_title": "Collect records from the second hospital",
        }
    )
    mock_plan(monkeypatch, [step])
    draft = propose(client, f).json()
    assert approve(client, f, draft).status_code == 200
    db, worker = runtime(database_url)
    try:
        await SupervisorActivities(db, worker.settings).coordinate_case(activation(f))
        async with db.sessions() as s:
            tasks = (
                await s.scalars(
                    select(Task).where(Task.tenant_id == UUID(f["firm_id"]), Task.kind == "AGENT")
                )
            ).all()
            assert len(tasks) == 2
            assert {t.progress_json["display_title"] for t in tasks} == {
                "Collect records from the first hospital",
                "Collect records from the second hospital",
            }
            coord = await s.scalar(
                select(CaseCoordination).where(CaseCoordination.tenant_id == UUID(f["firm_id"]))
            )
            assert all(t.parent_task_id == coord.group_task_id for t in tasks)
    finally:
        await db.close()


def test_old_unverified_plan_requires_new_review_before_approval(setup, database_url, monkeypatch):
    from psycopg.types.json import Jsonb

    client, f, _ = setup
    mock_plan(monkeypatch, [records(f)])
    draft = propose(client, f).json()
    with connect(database_url) as c:
        payload = c.execute(
            "SELECT content FROM sources WHERE id=%s", (UUID(draft["plan_id"]),)
        ).fetchone()[0]
        payload["router_version"] = 1
        c.execute(
            "UPDATE sources SET content=%s WHERE id=%s", (Jsonb(payload), UUID(draft["plan_id"]))
        )
    response = approve(client, f, draft)
    assert response.status_code == 409 and "Review the description again" in response.text
    assert count(database_url, "tasks", f) == 0

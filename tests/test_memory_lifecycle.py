"""Current controls stay small and temporal while original evidence remains inspectable."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from test_memory import activation, change, path, runtime
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.context import (
    EvidenceRequest,
    RetrievalRequest,
    RetrievalService,
    pack_context,
)
from agents_platform.memory_lifecycle import advance_lifecycles
from agents_platform.models import TaskMemoryItem

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def date_at(days=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def case_change(client, fixture, key, **fields):
    root = f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}"
    version = client.get(root + "/memory", headers=AUTH).json()["case_memory_version"]
    return client.post(
        root + "/restrictions",
        headers={**AUTH, "Idempotency-Key": key},
        json={"expected_version": version, "text": "Do not contact.", **fields},
    )


def test_duplicates_share_current_item_but_keep_separate_sources_and_replay(setup):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    first = change(client, fixture, task, key="one", text="Do not contact.").json()
    second = change(client, fixture, task, key="two", text="  DO NOT   contact. ").json()
    assert second["deduplicated"] and first["item_id"] == second["item_id"]
    assert first["source_id"] != second["source_id"]
    memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    assert len(memory["operational_items"]) == 1
    assert len(memory["history"]) == 1
    # Replay the exact original request despite the task now having a later version.
    original = {
        "expected_version": task["version"],
        "kind": "STOP_CONTACT",
        "text": "Do not contact.",
    }
    replay = client.post(
        path(fixture, task) + "/memory", headers={**AUTH, "Idempotency-Key": "one"}, json=original
    )
    assert replay.json()["replayed"] and replay.json()["item_id"] == first["item_id"]
    one = case_change(client, fixture, "case-one").json()
    two = case_change(client, fixture, "case-two", text="DO NOT CONTACT.").json()
    assert two["deduplicated"] and one["item_id"] == two["item_id"]
    provider = case_change(
        client,
        fixture,
        "provider",
        provider_contact_id=fixture["assignment"]["provider_contact_id"],
    ).json()
    assert provider["item_id"] != one["item_id"]  # Different scope must never merge.


async def test_supersession_preserves_chain_and_labels_old_retrieved_evidence(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    old = change(client, fixture, task, text="No outreach until authorization reviewed.").json()
    replacement = change(
        client,
        fixture,
        task,
        key="replace",
        operation="SUPERSEDE",
        item_id=old["item_id"],
        text="No outreach until corrected authorization reviewed.",
        reason="Corrected the required authorization reference.",
    ).json()
    memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    assert [i["id"] for i in memory["operational_items"]] == [replacement["item_id"]]
    previous = next(i for i in memory["history"] if i["id"] == old["item_id"])
    assert (
        previous["status"] == "SUPERSEDED"
        and previous["superseded_by_id"] == replacement["item_id"]
    )
    database, worker = runtime(database_url)
    try:
        service = RetrievalService(
            database, worker.settings, UUID(fixture["firm_id"]), UUID(task["case_id"])
        )
        found = await service.retrieve(RetrievalRequest(question=old["source_id"]))
        state = found["results"][0]["current_memory_state"][0]
        assert state["status"] == "SUPERSEDED" and not state["active"]
        read = await service.read(EvidenceRequest(source_id=UUID(old["source_id"])))
        assert read["current_memory_state"][0]["superseded_by_id"] == replacement["item_id"]
        assert "No outreach until authorization reviewed." in read["text"]
    finally:
        await database.close()


async def test_review_due_does_not_lift_old_hold_and_can_be_acknowledged(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    hold = case_change(client, fixture, "hold", review_at=date_at(-1)).json()
    case_root = f"/v1/firms/{fixture['firm_id']}/cases/{fixture['assignment']['case_id']}"
    review_url = case_root + "/memory/reviews"
    assert client.get(review_url).status_code in (401, 403)
    due = client.get(review_url, headers=AUTH).json()
    assert due["items"][0]["id"] == hold["item_id"]
    database, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(fixture, task)))["status"] == "BLOCKED"
    finally:
        await database.close()
    reviewed = case_change(
        client,
        fixture,
        "review",
        operation="REVIEW",
        item_id=hold["item_id"],
        text="Reviewed; keep this hold. Recheck next month.",
        review_at=date_at(30),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert client.get(review_url, headers=AUTH).json()["items"] == []
    memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    assert memory["operational_items"][0]["status"] == "OPEN"
    assert not memory["operational_items"][0]["review_due"]


async def test_effective_boundary_fences_send_and_timer_sweep_is_idempotent(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    future = datetime.now(timezone.utc) + timedelta(days=2)
    hold = change(
        client,
        fixture,
        task,
        effective_from=future.isoformat(),
        review_at=(future + timedelta(days=5)).isoformat(),
    )
    assert hold.status_code == 200, hold.text
    database, worker = runtime(database_url)
    try:
        prepared = await worker._prepare(activation(fixture, task))
        assert not prepared.context["memory"]["operational_items"]
        import agents_platform.memory_lifecycle as lifecycle

        monkeypatch.setattr(lifecycle, "now", lambda: future + timedelta(seconds=1))
        from agents.tool_context import ToolContext

        from agents_platform.execution import StaleExecution

        send = next(t for t in worker._tools(prepared) if t.name == "send_records_request")
        with pytest.raises(StaleExecution):
            await send.on_invoke_tool(
                ToolContext(
                    context=None, tool_name=send.name, tool_call_id="future", tool_arguments="{}"
                ),
                "{}",
            )
        assert await advance_lifecycles(database) == 1
        assert await advance_lifecycles(database) == 0
        memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
        assert memory["operational_items"][0]["active"]
        assert not memory["operational_items"][0]["review_due"]
        # Activation and later review are distinct timer events even at the same rule revision.
        monkeypatch.setattr(lifecycle, "now", lambda: future + timedelta(days=6))
        assert await advance_lifecycles(database) == 1
        assert await advance_lifecycles(database) == 0
        memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
        assert memory["operational_items"][0]["review_due"]
    finally:
        await database.close()


async def test_only_notes_expire_and_optional_notes_can_leave_the_briefing(
    setup, database_url, monkeypatch
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    assert change(client, fixture, task, expires_at=expires.isoformat()).status_code == 422
    assert (
        change(client, fixture, task, kind="OBLIGATION", expires_at=expires.isoformat()).status_code
        == 422
    )
    added = change(
        client,
        fixture,
        task,
        kind="NOTE",
        text="Temporary supporting note.",
        expires_at=expires.isoformat(),
    )
    assert added.status_code == 200, added.text
    memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    assert not memory["operational_items"] and len(memory["optional_notes"]) == 1
    packed = pack_context(
        {
            "memory": {
                "operational_items": [{"kind": "STOP_CONTACT", "text": "Keep"}],
                "optional_notes": [{"text": "n" * 2000}],
            }
        },
        100,
    )
    assert packed["memory"]["operational_items"][0]["text"] == "Keep"
    assert not packed["memory"]["optional_notes"]
    import agents_platform.memory_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "now", lambda: expires + timedelta(seconds=1))
    replay = client.post(
        path(fixture, task) + "/memory",
        headers={**AUTH, "Idempotency-Key": "hold"},
        json={
            "expected_version": task["version"],
            "kind": "NOTE",
            "text": "Temporary supporting note.",
            "expires_at": expires.isoformat(),
        },
    )
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert not client.get(path(fixture, task) + "/memory", headers=AUTH).json()["optional_notes"]
    database, _ = runtime(database_url)
    try:
        assert await advance_lifecycles(database) == 1
        assert await advance_lifecycles(database) == 0
        async with database.sessions() as session:
            row = await session.get(TaskMemoryItem, UUID(added.json()["item_id"]))
            assert row.status == "EXPIRED"
    finally:
        await database.close()


def test_resolved_history_does_not_consume_active_memory_capacity_and_is_paginated(
    setup, database_url
):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    hold = change(client, fixture, task).json()
    with connect(database_url) as connection:
        for _ in range(105):
            connection.execute(
                """INSERT INTO task_memory_items
                (id,tenant_id,case_id,task_id,kind,text,status,revision,source_id)
                VALUES (%s,%s,%s,%s,'OBLIGATION','Historical obligation','RESOLVED',2,%s)""",
                (
                    uuid4(),
                    UUID(fixture["firm_id"]),
                    UUID(task["case_id"]),
                    UUID(task["id"]),
                    UUID(hold["source_id"]),
                ),
            )
    added = change(
        client, fixture, task, key="new", kind="OBLIGATION", text="New active obligation."
    )
    assert added.status_code == 200, added.text
    memory = client.get(path(fixture, task) + "/memory?history_limit=10", headers=AUTH).json()
    assert len(memory["history"]) == 10 and memory["history_next_offset"] == 10
    assert len(memory["operational_items"]) == 2
    with connect(database_url) as connection:
        assert not connection.execute(
            "SELECT progress_json ? 'memory_items' FROM tasks WHERE id=%s", (UUID(task["id"]),)
        ).fetchone()[0]


def test_replacement_cannot_change_kind_or_scope_or_guess_future_lifting(setup):
    client, fixture, other = setup
    task = post(client, fixture).json()
    hold = change(client, fixture, task).json()
    response = change(
        client,
        fixture,
        task,
        key="bad",
        operation="SUPERSEDE",
        item_id=hold["item_id"],
        kind="NOTE",
        reason="Change",
        text="Informational",
    )
    assert response.status_code == 422
    response = change(
        client,
        fixture,
        task,
        key="future",
        operation="SUPERSEDE",
        item_id=hold["item_id"],
        reason="Change",
        effective_from=date_at(2),
    )
    assert response.status_code == 422
    case_hold = case_change(client, fixture, "case").json()
    replacement = case_change(
        client,
        fixture,
        "wrong-provider",
        operation="SUPERSEDE",
        item_id=case_hold["item_id"],
        reason="Change",
        provider_contact_id=fixture["assignment"]["provider_contact_id"],
    )
    assert replacement.status_code == 422
    other_task = post(client, other).json()
    response = change(
        client,
        other,
        other_task,
        key="foreign",
        operation="RESOLVE",
        item_id=hold["item_id"],
        kind=None,
    )
    assert response.status_code == 404


def test_instruction_retirement_preserves_revision_history_and_current_rules(setup):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    memory = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    instruction = memory["instructions"][0]
    url = f"/v1/firms/{fixture['firm_id']}/instructions/{instruction['id']}/revisions"
    response = client.post(
        url,
        headers=AUTH,
        json={
            "expected_revision": instruction["revision"],
            "text": instruction["text"],
            "reason": "Procedure retired after review.",
            "status": "RETIRED",
        },
    )
    assert response.status_code == 200, response.text
    current = client.get(path(fixture, task) + "/memory", headers=AUTH).json()
    assert instruction["id"] not in [i["id"] for i in current["instructions"]]
    history = client.get(url, headers=AUTH).json()
    assert history["current"]["status"] == "RETIRED"
    assert [i["status"] for i in history["history"]] == ["ACTIVE", "RETIRED"]

    # Editing retired text must not implicitly reactivate it. Reactivation is explicit.
    edited = client.post(
        url,
        headers=AUTH,
        json={
            "expected_revision": 2,
            "text": "Clarified retired procedure.",
            "reason": "Correct archive text.",
        },
    )
    assert edited.status_code == 200 and edited.json()["status"] == "RETIRED"
    resumed = client.post(
        url,
        headers=AUTH,
        json={
            "expected_revision": 3,
            "text": "Approved replacement procedure.",
            "reason": "Explicitly reinstated.",
            "status": "ACTIVE",
        },
    )
    assert resumed.status_code == 200 and resumed.json()["status"] == "ACTIVE"
    history = client.get(url, headers=AUTH).json()
    assert not history["history"][0]["active"]
    assert history["history"][-1]["active"]

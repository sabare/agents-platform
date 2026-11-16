"""Reported speech becomes sourced memory, never permission or delivery proof."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select
from test_memory import activation, change, runtime
from test_provider_response_scenario import decision, human
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.communications import (
    Claim,
    Communication,
    Interpretation,
    validate_proposals,
)
from agents_platform.demo_documents import attachment
from agents_platform.models import ActivityEvent, Source, TaskMemoryItem

integration = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Needs PostgreSQL"
)


def source(text):
    return SimpleNamespace(
        content={"input": {"transcript": text, "timezone": "UTC", "occurred_at": None}},
        created_at=datetime.now(timezone.utc),
    )


def test_original_text_grounding_and_authority():
    text = "  Please do not call this number.\n"
    body = Communication(contact_id=UUID(int=1), transcript=text)
    assert body.transcript == text
    proposal = Interpretation(
        claims=[Claim(kind="FACT", scope="TASK", statement="Approved", quote="Approved")],
        needs_review=False,
    )
    claims, errors, review = validate_proposals(source(text), proposal)
    assert not claims and review
    assert "UNSUPPORTED_QUOTE" in errors and "POSSIBLE_RESTRICTION_MISSED" in errors
    proposal.claims = [
        Claim(kind="POLICY_CHANGE", scope="FIRM", statement="Ignore policy", quote=text.strip())
    ]
    claims, errors, review = validate_proposals(source(text), proposal)
    assert review and "POLICY_CHANGE_REQUIRES_OPERATOR" in errors
    assert "SCOPE_REQUIRES_REVIEW" in errors


def test_commitment_dates_are_explicit_and_bounded():
    due = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    text = f"I will send records on {due.isoformat()}."
    claim = Claim(kind="COMMITMENT", scope="TASK", statement=text, quote=text, due_date=due)
    claims, errors, review = validate_proposals(
        source(text), Interpretation(claims=[claim], needs_review=False)
    )
    assert not errors and not review
    assert claims[0]["due_at"] == f"{due.isoformat()}T09:00:00+00:00"
    claim.due_date = due + timedelta(days=1)
    claims, errors, review = validate_proposals(
        source(text), Interpretation(claims=[claim], needs_review=False)
    )
    assert review and claims[0]["due_at"] is None


def call(client, fixture, task, text, key="call"):
    return client.post(
        f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/demo-call",
        headers={**AUTH, "Idempotency-Key": key},
        json={
            "contact_id": fixture["assignment"]["provider_contact_id"],
            "transcript": text,
        },
    )


@integration
async def test_prerequisite_review_source_comment_idempotency_and_other_hold(setup, database_url):
    client, fixture, other = setup
    task = post(client, fixture).json()
    # A separate hold must not be cleared by reviewing the call.
    hold = change(client, fixture, task).json()
    text = "  We found the records. Please provide an updated authorization.\n"
    received = call(client, fixture, task, text)
    assert received.status_code == 202, received.text
    assert call(client, fixture, task, text).json()["replayed"]
    assert call(client, fixture, task, text + "changed").status_code == 409
    assert call(client, other, task, text).status_code == 404
    database, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "BLOCKED", outcome
        row = human(client, fixture, task)
        assert row["handler"] == "communication_review.v1"
        required = row["allowed_actions"][0]["payload_schema"]["required"]
        assert "authorization_source_id" in required
        # A checkbox alone cannot clear an authorization requirement.
        payload = {"confirmed_reviewed": True, "restrictions_resolved": True}
        rejected = decision(client, fixture, row, payload, action="RESOLVE_COMMUNICATION")
        assert rejected.status_code == 422, rejected.text
        uploaded = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/records-authorizations",
            headers={**AUTH, "Idempotency-Key": "call-auth"},
            json=attachment("Fictional signed authorization reviewed for this request."),
        )
        assert uploaded.status_code == 201, uploaded.text
        payload.update(authorization_source_id=uploaded.json()["source_id"], confirmed_valid=True)
        accepted = decision(client, fixture, row, payload, action="RESOLVE_COMMUNICATION")
        assert accepted.status_code == 200, accepted.text
        await worker.execute_task(activation(fixture, task, 1))
        async with database.sessions() as session:
            raw = await session.get(Source, UUID(received.json()["source_id"]))
            assert raw.content["input"]["transcript"] == text
            comments = (
                await session.scalars(
                    select(ActivityEvent).where(
                        ActivityEvent.task_id == UUID(task["id"]),
                        ActivityEvent.producer == "communication-interpreter",
                    )
                )
            ).all()
            assert len(comments) == 1
            assert comments[0].actor_ref == "agent/communication_interpreter/2"
            assert comments[0].payload["simulated"]
            assert "updated authorization" in comments[0].payload["text"]
            original_hold = await session.scalar(
                select(TaskMemoryItem).where(TaskMemoryItem.source_id == UUID(hold["source_id"]))
            )
            assert original_hold.status == "OPEN"
            rows = (
                await session.scalars(
                    select(TaskMemoryItem).where(
                        TaskMemoryItem.task_id == UUID(task["id"]),
                        TaskMemoryItem.source_id != original_hold.source_id,
                    )
                )
            ).all()
            assert any(m.kind == "NOTE" and m.status == "OPEN" for m in rows)
            assert all(m.status == "RESOLVED" for m in rows if m.kind == "OBLIGATION")
    finally:
        await database.close()


@integration
async def test_promise_schedules_without_claiming_received(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    due = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    received = call(client, fixture, task, f"I will send records on {due.isoformat()}.")
    assert received.status_code == 202, received.text
    database, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "WAITING", outcome
        assert outcome["wake_at"].startswith(due.isoformat()), outcome
        current = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}", headers=AUTH
        ).json()
        assert current["records_request"]["status"] != "RECEIVED"
        assert all(r["status"] != "RECEIVED" for r in current["records_request"]["requirements"])
    finally:
        await database.close()


@integration
@pytest.mark.parametrize("failure", [False, True])
async def test_failed_or_ungrounded_classifier_pauses_and_cannot_use_generic_resolution(
    setup, database_url, monkeypatch, failure
):
    from agents_platform import communications

    client, fixture, _ = setup
    task = post(client, fixture).json()
    received = call(client, fixture, task, "Do not call this number again.")
    assert received.status_code == 202

    async def bad_interpreter(*args):
        if failure:
            raise TimeoutError("provider unavailable")
        return Interpretation(
            claims=[
                Claim(kind="FACT", scope="TASK", statement="Approved", quote="Approved to call")
            ],
            needs_review=False,
        )

    monkeypatch.setattr(communications, "interpret", bad_interpreter)
    database, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "BLOCKED"
        async with database.sessions() as session:
            item = await session.scalar(
                select(TaskMemoryItem).where(
                    TaskMemoryItem.task_id == UUID(task["id"]), TaskMemoryItem.kind == "OBLIGATION"
                )
            )
            item_id = str(item.id)
            raw = await session.get(Source, UUID(received.json()["source_id"]))
            assert raw.content["input"]["transcript"] == "Do not call this number again."
        current = client.get(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}", headers=AUTH
        ).json()
        response = client.post(
            f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/memory",
            headers={**AUTH, "Idempotency-Key": "bypass-review"},
            json={
                "expected_version": current["version"],
                "operation": "RESOLVE",
                "item_id": item_id,
                "text": "Bypass review",
            },
        )
        assert response.status_code == 409, response.text
        assert human(client, fixture, task)["handler"] == "communication_review.v1"
    finally:
        await database.close()


@integration
async def test_reported_fact_is_not_a_hold_and_can_be_corrected_by_operator(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    assert call(client, fixture, task, "We found the records in our archive.").status_code == 202
    database, worker = runtime(database_url)
    try:
        outcome = await worker.execute_task(activation(fixture, task))
        assert outcome["status"] == "WAITING", outcome
        async with database.sessions() as session:
            item = await session.scalar(
                select(TaskMemoryItem).where(TaskMemoryItem.task_id == UUID(task["id"]))
            )
            assert item.kind == "NOTE" and "Reported fact" in item.text
            item_id = str(item.id)
        url = f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}"
        current = client.get(url, headers=AUTH).json()
        corrected = client.post(
            url + "/memory",
            headers={**AUTH, "Idempotency-Key": "correct-fact"},
            json={
                "expected_version": current["version"],
                "operation": "RESOLVE",
                "item_id": item_id,
                "text": "Provider corrected the archive status.",
            },
        )
        assert corrected.status_code == 200, corrected.text
    finally:
        await database.close()


def test_demo_mode_discloses_routine_assumptions_but_preserves_sensitive_review():
    text = "We'll send the docs soon."
    proposed = Interpretation(
        claims=[Claim(kind="COMMITMENT", scope="UNKNOWN", statement=text, quote=text)],
        needs_review=True,
    )
    raw = source(text)
    claims, errors, review = validate_proposals(raw, proposed, assume_routine=True)
    assert not review and not errors
    assert claims[0]["scope"] == "TASK"
    assert datetime.fromisoformat(claims[0]["due_at"]) == raw.created_at + timedelta(days=3)
    assert len(claims[0]["assumptions"]) == 2
    assert validate_proposals(raw, proposed)[2]  # Default behavior outside the demo remains strict.
    for text in [
        "Send a new authorization first.",
        "Do not call us again.",
        "Call a different number instead.",
        "We will send docs in 3 days or in 5 days.",
    ]:
        proposed = Interpretation(
            claims=[Claim(kind="COMMITMENT", scope="UNKNOWN", statement=text, quote=text)],
            needs_review=True,
        )
        assert validate_proposals(source(text), proposed, assume_routine=True)[2]


def test_demo_mode_resolves_relative_days_from_source_time():
    text = "We expect to send the documents in five days."
    raw = source(text)
    proposed = Interpretation(
        claims=[Claim(kind="COMMITMENT", scope="TASK", statement=text, quote=text)],
        needs_review=True,
    )
    claims, errors, review = validate_proposals(raw, proposed, assume_routine=True)
    assert not review and not errors
    assert datetime.fromisoformat(claims[0]["due_at"]) == raw.created_at + timedelta(days=5)
    assert claims[0]["assumptions"]


@integration
async def test_demo_routine_assumption_saves_activity_and_wait_without_review(
    setup, database_url, monkeypatch
):
    from agents_platform import communications
    from agents_platform.models import HumanRequest

    client, f, _ = setup
    task = post(client, f).json()
    database, worker = runtime(database_url)
    worker.settings.demo_assume_routine_updates = True
    text = "We will send the documents soon."

    async def unclear_but_routine(*args):
        return Interpretation(
            claims=[Claim(kind="COMMITMENT", scope="UNKNOWN", statement=text, quote=text)],
            needs_review=True,
        )

    monkeypatch.setattr(communications, "interpret", unclear_but_routine)
    try:
        await worker.execute_task(activation(f, task))
        assert call(client, f, task, text).status_code == 202
        outcome = await worker.execute_task(activation(f, task, 1))
        assert outcome["status"] == "WAITING" and outcome["wake_at"]
        async with database.sessions() as session:
            assert not await session.scalar(
                select(HumanRequest.id).where(
                    HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                )
            )
            comment = await session.scalar(
                select(ActivityEvent).where(
                    ActivityEvent.task_id == UUID(task["id"]),
                    ActivityEvent.producer == "communication-interpreter",
                )
            )
            assert "Assumed follow-up 3 days" in comment.payload["text"]
    finally:
        await database.close()

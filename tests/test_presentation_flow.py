"""The exact records walkthrough: callback, partial delivery, approval, early final delivery."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import select
from test_communications import call
from test_memory import activation, runtime
from test_provider_response_scenario import decision, human, ingest, task_path
from test_task_persistence import AUTH, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

from agents_platform.demo_documents import attachment, pdf_attachment
from agents_platform.models import HumanRequest, RecordsRequest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Needs PostgreSQL"
)


async def test_records_walkthrough_with_future_callback_then_early_documents(setup, database_url):
    client, f, _ = setup
    task = post(
        client,
        f,
        body={
            **f["assignment"],
            "requirements": [
                {"record_type": k, "all_dates": True} for k in ["RECORDS", "SURGICAL_NOTES"]
            ],
        },
    ).json()
    database, worker = runtime(database_url)
    try:
        assert (await worker.execute_task(activation(f, task)))["status"] == "WAITING"
        assert call(client, f, task, "Please call us back in three days.").status_code == 202
        waiting = await worker.execute_task(activation(f, task, 1))
        assert waiting["status"] == "WAITING"
        assert ingest(client, task, files=[pdf_attachment()]).status_code == 202
        assert (await worker.execute_task(activation(f, task, 2)))["status"] == "BLOCKED"
        uploaded = client.post(
            task_path(task) + "/records-authorizations",
            headers={**AUTH, "Idempotency-Key": "presentation-auth"},
            json=attachment("Fictional authorization reviewed for this request."),
        )
        assert uploaded.status_code == 201, uploaded.text
        row = human(client, f, task)
        approved = decision(
            client,
            f,
            row,
            {
                "authorization_source_id": uploaded.json()["source_id"],
                "confirmed_valid": True,
                "response_due_at": (datetime.now(timezone.utc) + timedelta(days=4)).isoformat(),
            },
        )
        assert approved.status_code == 200, approved.text
        assert (await worker.execute_task(activation(f, task, 3)))["status"] == "WAITING"
        async with database.sessions() as session:
            from agents_platform.models import HumanContinuation

            continuation = await session.scalar(
                select(HumanContinuation).where(HumanContinuation.task_id == UUID(task["id"]))
            )
            assert continuation.status == "PENDING"  # Approval must not override callback timing.
        notes = pdf_attachment("surgical notes", pages=2)
        received = ingest(
            client,
            task,
            message="The surgical notes are attached. General records were supplied earlier.",
            files=[notes],
            key="final-notes",
        )
        assert received.status_code == 202, received.text
        final = await worker.execute_task(activation(f, task, 4))
        assert final["status"] == "COMPLETED", final
        async with database.sessions() as session:
            assert not await session.scalar(
                select(HumanRequest.id).where(
                    HumanRequest.task_id == UUID(task["id"]), HumanRequest.status == "OPEN"
                )
            )
            request = await session.scalar(
                select(RecordsRequest).where(RecordsRequest.task_id == UUID(task["id"]))
            )
            assert request.reminder_count == 0
    finally:
        await database.close()

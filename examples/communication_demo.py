"""Exercise transcript ingestion on real Temporal; --live uses OpenAI classification."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url
from temporalio.worker import Worker

from agents_platform.api import create_app
from agents_platform.config import Settings
from agents_platform.db import Database
from agents_platform.demo_documents import attachment
from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import ActivityEvent, Source, Task, TaskMemoryItem
from agents_platform.observability import configure_observability
from agents_platform.orchestration.client import connect_temporal
from agents_platform.orchestration.workflows import TaskWorkflow
from agents_platform.seed import seed_demo


async def journey(settings, output):
    database = Database(settings.database_url)
    telemetry = configure_observability(settings, "communication-demo")
    async with database.sessions() as session:
        fixture = await seed_demo(session, uuid4())
    firm = UUID(fixture["firm_id"])
    temporal = await connect_temporal(settings)
    queue = "communication-demo-" + uuid4().hex
    activities = TaskActivities(database, settings)
    options = dict(
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[activities.execute_task, activities.fail_task],
        graceful_shutdown_timeout=timedelta(seconds=5),
    )
    workflows, checks, report = [], {}, {"mode": settings.agent_mode}
    root = f"/v1/firms/{firm}"
    try:
        with TestClient(create_app(settings)) as client:

            async def request(method, path, **kwargs):
                response = await asyncio.to_thread(
                    getattr(client, method),
                    path,
                    headers={
                        "Authorization": "Bearer " + settings.operator_api_key.get_secret_value(),
                        **kwargs.pop("headers", {}),
                    },
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()

            async def wait(task_id, status, *, previous=None, future=False):
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    for _ in range(4):
                        if not await dispatch_once(database, temporal, queue, firm):
                            break
                    async with database.sessions() as session:
                        row = await session.get(Task, UUID(task_id))
                        if row.status == status and (
                            future
                            or (
                                row.progress_json.get("last_execution_id")
                                and row.progress_json.get("last_execution_id") != previous
                            )
                        ):
                            return
                        if row.status == "FAILED":
                            raise RuntimeError("Worker failed")
                    await asyncio.sleep(0.25)
                raise RuntimeError(f"Timed out waiting for {status}")

            async def new_task(key):
                row = await request(
                    "post",
                    root + "/tasks/medical-records",
                    headers={"Idempotency-Key": key},
                    json={**fixture["assignment"], "agent_version": 4},
                )
                workflows.append(row["workflow_id"])
                return row

            async def call(task, text, key):
                return await request(
                    "post",
                    root + f"/tasks/{task['id']}/demo-call",
                    headers={"Idempotency-Key": key},
                    json={
                        "contact_id": fixture["assignment"]["provider_contact_id"],
                        "transcript": text,
                        "timezone": "UTC",
                    },
                )

            task = await new_task("restriction-task")
            text = (
                "  For this records request, we found the records in our archive. "
                "Do not call us again about this request until we receive an updated "
                "authorization. Please provide an updated authorization for this request.\n"
            )
            raw = await call(task, text, "restriction-call")
            replay = await call(task, text, "restriction-call")
            checks["call_idempotent"] = replay["replayed"]
            async with Worker(temporal, **options):
                await wait(task["id"], "BLOCKED")
                requests = await request(
                    "get",
                    root + "/human-requests",
                    params={"task_id": task["id"], "status": "OPEN"},
                )
                review = next(
                    r for r in requests["items"] if r["handler"] == "communication_review.v1"
                )
                checks["human_review_created"] = bool(review)
                async with database.sessions() as session:
                    original = await session.get(Source, UUID(raw["source_id"]))
                    analysis = await session.scalar(
                        select(Source).where(
                            Source.source_key == "communication-analysis/" + raw["source_id"]
                        )
                    )
                    kinds = {c["kind"] for c in analysis.content["accepted_claims"]}
                    checks["model_extracted_fact_restriction_prerequisite"] = {
                        "FACT",
                        "RESTRICTION",
                        "PREREQUISITE",
                    } <= kinds
                    checks["original_preserved"] = original.content["input"]["transcript"] == text
                    report["restriction_analysis"] = analysis.content
                    comments = (
                        await session.scalars(
                            select(ActivityEvent).where(
                                ActivityEvent.task_id == UUID(task["id"]),
                                ActivityEvent.producer == "communication-interpreter",
                            )
                        )
                    ).all()
                    checks["one_sourced_agent_comment"] = (
                        len(comments) == 1
                        and comments[0].payload["communication_source_id"] == raw["source_id"]
                        and comments[0].actor_ref == "agent/communication_interpreter/1"
                    )
                    items = (
                        await session.scalars(
                            select(TaskMemoryItem).where(
                                TaskMemoryItem.task_id == UUID(task["id"]),
                                TaskMemoryItem.status == "OPEN",
                            )
                        )
                    ).all()
                    checks["protective_memory_saved"] = {"STOP_CONTACT", "OBLIGATION"} <= {
                        item.kind for item in items
                    }
            before = await request("get", root + f"/tasks/{task['id']}")
            # Persist the decision with no worker. Restarting must resume the same task.
            document = await request(
                "post",
                root + f"/tasks/{task['id']}/records-authorizations",
                headers={"Idempotency-Key": "authorization"},
                json=attachment("Fictional signed authorization; reviewed."),
            )
            await request(
                "post",
                root + f"/human-requests/{review['id']}/decisions",
                headers={"Idempotency-Key": "review"},
                json={
                    "expected_task_version": review["task_version"],
                    "expected_request_version": review["version"],
                    "action": "RESOLVE_COMMUNICATION",
                    "reason": "Reviewed original call and valid updated authorization.",
                    "payload": {
                        "confirmed_reviewed": True,
                        "restrictions_resolved": True,
                        "authorization_source_id": document["source_id"],
                        "confirmed_valid": True,
                    },
                },
            )
            async with Worker(temporal, **options):
                await wait(
                    task["id"], "WAITING", previous=before["progress_json"]["last_execution_id"]
                )
                current = await request("get", root + f"/tasks/{task['id']}")
                checks["resumed_same_task_after_restart"] = current["id"] == task["id"]
                checks["approval_is_not_delivery"] = (
                    current["records_request"]["status"] != "RECEIVED"
                )
                promise = await new_task("commitment-task")
                due = (datetime.now(timezone.utc) + timedelta(days=3)).date()
                await call(
                    promise,
                    f"For this records request, I will send the records on {due}.",
                    "promise-call",
                )
                await wait(promise["id"], "WAITING", future=True)
                current = await request("get", root + f"/tasks/{promise['id']}")
                checks["promise_not_delivery"] = current["records_request"]["status"] != "RECEIVED"
                async with database.sessions() as session:
                    item = await session.scalar(
                        select(TaskMemoryItem).where(
                            TaskMemoryItem.task_id == UUID(promise["id"]),
                            TaskMemoryItem.due_at.is_not(None),
                        )
                    )
                    checks["dated_commitment_saved"] = bool(item and item.due_at.date() == due)
                    row = await session.get(Task, UUID(promise["id"]))
                    report["promise_workflow_id"] = row.workflow_id
                description = await temporal.get_workflow_handle(promise["workflow_id"]).describe()
                checks["durable_workflow_waiting"] = description.status.name == "RUNNING"
        report.update(checks=checks, passed=all(checks.values()))
        output.mkdir(parents=True, exist_ok=True)
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {"mode": settings.agent_mode, "checks": checks, "passed": report["passed"]},
                indent=2,
            )
        )
        if not report["passed"]:
            raise RuntimeError("Behavior checks failed")
    finally:
        for identity in workflows:
            try:
                await temporal.get_workflow_handle(identity).terminate("Disposable demo cleanup")
            except Exception:
                pass
        await asyncio.to_thread(telemetry.flush)
        await database.close()
        telemetry.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("work/communication-demo"))
    args = parser.parse_args()
    load_dotenv()
    settings = Settings(agent_mode="openai" if args.live else "scripted", otel_enabled=True)
    admin = make_url(settings.migration_database_url or settings.database_url)
    name = "agents_communication_" + uuid4().hex[:16]
    with psycopg.connect(
        admin.set(drivername="postgresql").render_as_string(hide_password=False), autocommit=True
    ) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            url = admin.set(database=name).render_as_string(hide_password=False)
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env={**os.environ, "DATABASE_URL": url},
                check=True,
            )
            asyncio.run(journey(settings.model_copy(update={"database_url": url}), args.output))
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    main()

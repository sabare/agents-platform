"""Run the hospital scenario on real Temporal. Default offline; --live uses OpenAI."""

import argparse
import asyncio
import base64
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
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from temporalio.worker import Worker

from agents_platform.api import create_app
from agents_platform.audit_integrity import verify
from agents_platform.config import Settings
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.demo_documents import MESSAGE, attachment, pdf_attachment
from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import (
    CaseCoordination,
    Execution,
    HumanContinuation,
    HumanRequest,
    Source,
    Task,
)
from agents_platform.observability import configure_observability
from agents_platform.orchestration.client import connect_temporal
from agents_platform.orchestration.workflows import CaseWorkflow, TaskWorkflow
from agents_platform.seed import seed_demo


async def journey(settings, output):
    database = Database(settings.database_url)
    telemetry = configure_observability(settings, "hospital-scenario")
    async with database.sessions() as session:
        fixture = await seed_demo(session, uuid4())
    firm = UUID(fixture["firm_id"])
    case = UUID(fixture["assignment"]["case_id"])
    temporal = await connect_temporal(settings)
    queue = "hospital-scenario-" + uuid4().hex
    specialist, supervisor = (
        TaskActivities(database, settings),
        SupervisorActivities(database, settings),
    )
    worker_options = dict(
        task_queue=queue,
        workflows=[CaseWorkflow, TaskWorkflow],
        activities=[
            specialist.execute_task,
            specialist.fail_task,
            supervisor.coordinate_case,
            supervisor.fail_supervisor,
        ],
        graceful_shutdown_timeout=timedelta(seconds=5),
    )
    checks = {}
    workflow_ids = []
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "mode": settings.agent_mode,
        "checks": checks,
        "limitations": [
            "Fictional labelled PDFs; no OCR, signature verification or real hospital transport.",
            "One scenario with explicit assertions, not a broad quality benchmark.",
        ],
    }
    try:
        with TestClient(create_app(settings)) as api:

            async def request(method, path, **kwargs):
                headers = {
                    "Authorization": "Bearer " + settings.operator_api_key.get_secret_value(),
                    **kwargs.pop("headers", {}),
                }
                response = await asyncio.to_thread(
                    getattr(api, method), path, headers=headers, **kwargs
                )
                response.raise_for_status()
                return response.json()

            async def wait_for(predicate):
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    for _ in range(3):
                        if not await dispatch_once(database, temporal, queue, firm):
                            break
                    async with database.sessions() as session:
                        tasks = (
                            await session.scalars(
                                select(Task).where(Task.tenant_id == firm, Task.kind == "AGENT")
                            )
                        ).all()
                        task = tasks[0] if tasks else None
                        coord = await session.scalar(
                            select(CaseCoordination).where(
                                CaseCoordination.tenant_id == firm, CaseCoordination.case_id == case
                            )
                        )
                        if task and task.status == "FAILED":
                            raise RuntimeError("Specialist failed; inspect its execution trace")
                        if not task and coord and coord.status == "NEEDS_REVIEW":
                            raise RuntimeError(
                                "Supervisor could not delegate the approved assignment"
                            )
                        if task and task.workflow_id not in workflow_ids:
                            workflow_ids.append(task.workflow_id)
                        if coord and coord.workflow_id not in workflow_ids:
                            workflow_ids.append(coord.workflow_id)
                        if task and await predicate(session, task, coord):
                            return task, coord
                    await asyncio.sleep(0.2)
                raise RuntimeError("Scenario timed out; inspect saved traces")

            async def sent(session, task, coord):
                return task.status == "WAITING" and task.progress_json.get("last_execution_id")

            async def blocked(session, task, coord):
                run = (
                    await session.get(Execution, UUID(task.progress_json["last_execution_id"]))
                    if task.progress_json.get("last_execution_id")
                    else None
                )
                return (
                    task.status == "BLOCKED"
                    and run
                    and run.context_json.get("domain", {}).get("state", {}).get("provider_blocker")
                    and coord.status == "NEEDS_REVIEW"
                )

            async with Worker(temporal, **worker_options):
                await request(
                    "post",
                    f"/v1/firms/{firm}/cases/{case}/supervisor",
                    headers={"Idempotency-Key": "hospital-objective"},
                    json={
                        "objective": (
                            "Collect general records and surgical notes from the hospital."
                        ),
                        "approved_assignments": [
                            {
                                "agent_key": "medical_records_collector",
                                "agent_version": 5,
                                "objective": (
                                    "Collect general records and surgical notes from the hospital."
                                ),
                                "assignment": {
                                    "patient_contact_id": fixture["assignment"][
                                        "patient_contact_id"
                                    ],
                                    "provider_contact_id": fixture["assignment"][
                                        "provider_contact_id"
                                    ],
                                    "requirements": [
                                        {"record_type": kind, "all_dates": True}
                                        for kind in ("RECORDS", "SURGICAL_NOTES")
                                    ],
                                },
                            }
                        ],
                    },
                )
                task, coord = await wait_for(sent)
                root = f"/v1/firms/{firm}/tasks/{task.id}"
                first = pdf_attachment()
                (output / "hospital-records-40-pages.pdf").write_bytes(
                    base64.b64decode(first["content_base64"])
                )
                received = await request(
                    "post",
                    root + "/provider-responses",
                    headers={"Idempotency-Key": "hospital-response"},
                    json={"message": MESSAGE, "attachments": [first]},
                )
                task, coord = await wait_for(blocked)
                view = await request("get", root)
                states = {
                    r["record_type"]: r["status"] for r in view["records_request"]["requirements"]
                }
                checks["received_general_records_only"] = states == {
                    "RECORDS": "RECEIVED",
                    "SURGICAL_NOTES": "OUTSTANDING",
                }
                checks["no_early_completion"] = (
                    task.status == "BLOCKED" and coord.status != "SATISFIED"
                )
                async with database.sessions() as session:
                    raw = await session.get(Source, UUID(received["source_id"]))
                    analysis = await session.scalar(
                        select(Source).where(
                            Source.tenant_id == firm,
                            Source.source_key == f"provider-analysis/{raw.id}",
                        )
                    )
                    run = await session.get(
                        Execution, UUID(task.progress_json["last_execution_id"])
                    )
                    missing_ids = {
                        str(r["id"])
                        for r in view["records_request"]["requirements"]
                        if r["record_type"] == "SURGICAL_NOTES"
                    }
                    proposal = analysis.content["extraction"]
                    checks["original_bytes_and_message_preserved"] = raw.content[
                        "message"
                    ] == MESSAGE and raw.content["attachments"] == [first]
                    checks["forty_pages_parsed"] = (
                        analysis.content["documents"][0]["page_count"] == 40
                    )
                    checks["extractor_identified_missing_notes_and_authorization"] = (
                        proposal["authorization_required"]
                        and {r["requirement_id"] for r in proposal["missing"]} == missing_ids
                    )
                    checks["agent_requested_human_before_backend_validation"] = (
                        run.usage_json or {}
                    ).get("agent_proposed_action") == "needs_human"
                    report["observed"] = {
                        "coverage_after_response": states,
                        "extractor_output": proposal,
                        "agent_proposed_action": (run.usage_json or {}).get(
                            "agent_proposed_action"
                        ),
                        "backend_task_status": task.status,
                    }
                    report["blocked_execution_trace"] = run.context_json.get("observability", {})
                    human = await session.scalar(
                        select(HumanRequest).where(
                            HumanRequest.tenant_id == firm,
                            HumanRequest.task_id == task.id,
                            HumanRequest.handler == "provider_response.v1",
                            HumanRequest.status == "OPEN",
                        )
                    )
                    human_id = human.id
                    checks["typed_human_request_created"] = bool(human)
            # Worker is stopped while blocked. The human decision is persisted first.
            auth = attachment(
                "Fictional updated authorization. The operator reviewed it for this request."
            )
            (output / "updated-authorization.txt").write_bytes(
                base64.b64decode(auth["content_base64"])
            )
            supplied = await request(
                "post",
                root + "/records-authorizations",
                headers={"Idempotency-Key": "updated-auth"},
                json=auth,
            )
            human = await request("get", f"/v1/firms/{firm}/human-requests/{human_id}")
            await request(
                "post",
                f"/v1/firms/{firm}/human-requests/{human_id}/decisions",
                headers={"Idempotency-Key": "review-auth"},
                json={
                    "expected_task_version": human["task_version"],
                    "expected_request_version": human["version"],
                    "action": "PROVIDE_UPDATED_AUTHORIZATION",
                    "reason": "Updated authorization reviewed. Follow up once for surgical notes.",
                    "payload": {
                        "authorization_source_id": supplied["source_id"],
                        "confirmed_valid": True,
                        "response_due_at": (
                            datetime.now(timezone.utc) + timedelta(days=3)
                        ).isoformat(),
                    },
                },
            )
            async with Worker(temporal, **worker_options):

                async def resumed(session, task, coord):
                    row = await session.scalar(
                        select(HumanContinuation).where(
                            HumanContinuation.tenant_id == firm,
                            HumanContinuation.task_id == task.id,
                            HumanContinuation.status == "APPLIED",
                        )
                    )
                    return task.status == "WAITING" and row is not None

                task, coord = await wait_for(resumed)
                async with database.sessions() as session:
                    row = await session.scalar(
                        select(HumanContinuation).where(
                            HumanContinuation.tenant_id == firm,
                            HumanContinuation.task_id == task.id,
                        )
                    )
                    effect = await session.get(Source, row.source_id)
                    checks["resumed_after_worker_restart"] = row.status == "APPLIED"
                    checks["follow_up_only_requests_missing_notes"] = [
                        r["record_type"] for r in effect.content["requested_items"]
                    ] == ["SURGICAL_NOTES"] and effect.content[
                        "authorization_source_id"
                    ] == supplied["source_id"]
                    checks["approval_did_not_complete_task"] = task.status == "WAITING"
                notes = pdf_attachment("surgical notes", 2)
                (output / "surgical-notes-2-pages.pdf").write_bytes(
                    base64.b64decode(notes["content_base64"])
                )
                await request(
                    "post",
                    root + "/provider-responses",
                    headers={"Idempotency-Key": "surgical-notes"},
                    json={
                        "message": "The requested surgical notes are now attached.",
                        "attachments": [notes],
                    },
                )

                async def completed(session, task, coord):
                    return task.status == "COMPLETED" and coord.status == "SATISFIED"

                task, coord = await wait_for(completed)
                checks["completes_only_after_notes_arrive"] = (
                    task.status == "COMPLETED" and coord.status == "SATISFIED"
                )
                async with database.sessions() as session:
                    types = set(
                        (
                            await session.scalars(
                                text(
                                    "SELECT event->>'event_type' FROM audit.events "
                                    "WHERE stream_id=:firm"
                                ),
                                {"firm": firm},
                            )
                        ).all()
                    )
                    checks["audit_covers_authority_approval_and_effect"] = {
                        "authorization.decision",
                        "approval.decision",
                        "external_effect.result",
                    } <= types
        with psycopg.connect(
            settings.database_url.replace("postgresql+psycopg:", "postgresql:")
        ) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            checkpoint, _ = verify(connection, firm)
            report["audit_checkpoint"] = checkpoint
            checks["audit_chain_verified"] = True
        await asyncio.to_thread(telemetry.flush)
        report["passed"] = all(checks.values())
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        if not report["passed"]:
            raise RuntimeError("Scenario behavior checks failed; see results.json")
    finally:
        for identity in workflow_ids:
            try:
                await temporal.get_workflow_handle(identity).terminate(
                    "Disposable hospital scenario cleanup"
                )
            except Exception:
                pass
        await database.close()
        telemetry.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("work/hospital-scenario"))
    args = parser.parse_args()
    load_dotenv()
    settings = Settings(agent_mode="openai" if args.live else "scripted", otel_enabled=True)
    admin = make_url(settings.migration_database_url or settings.database_url)
    name = "agents_hospital_" + uuid4().hex[:16]
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

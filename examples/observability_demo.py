"""Export a complete supervisor/worker journey to local Jaeger and Langfuse.

Uses a disposable PostgreSQL database and isolated Temporal queue; no test firms are
left in the application database. Default model is scripted; --live makes real API calls.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
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
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.dispatch import dispatch_once
from agents_platform.execution import TaskActivities
from agents_platform.models import CaseCoordination, Execution, Task
from agents_platform.observability import configure_observability
from agents_platform.orchestration.client import connect_temporal
from agents_platform.orchestration.workflows import CaseWorkflow, TaskWorkflow
from agents_platform.seed import seed_demo


async def journey(settings, output):
    telemetry = configure_observability(settings, "trace-demo")
    database = Database(settings.database_url)
    async with database.sessions() as session:
        fixture = await seed_demo(session, uuid4())
    firm_id = UUID(fixture["firm_id"])
    case_id = UUID(fixture["assignment"]["case_id"])
    supervisor, specialist = (
        SupervisorActivities(database, settings),
        TaskActivities(database, settings),
    )
    temporal = await connect_temporal(settings)
    queue = "observability-" + uuid4().hex
    task_id = None
    workflow_ids = []
    auth = {"Authorization": "Bearer " + settings.operator_api_key.get_secret_value()}
    try:
        with TestClient(create_app(settings)) as api:
            async with Worker(
                temporal,
                task_queue=queue,
                workflows=[CaseWorkflow, TaskWorkflow],
                activities=[
                    supervisor.coordinate_case,
                    supervisor.fail_supervisor,
                    specialist.execute_task,
                    specialist.fail_task,
                ],
            ):
                body = fixture["assignment"]
                response = api.post(
                    f"/v1/firms/{firm_id}/cases/{case_id}/supervisor",
                    headers={
                        **auth,
                        "Idempotency-Key": "trace-demo-objective",
                    },
                    json={
                        "objective": "Collect the approved fictional records and imaging.",
                        "approved_assignments": [
                            {
                                "agent_key": "medical_records_collector",
                                "agent_version": 4,
                                "objective": "Collect the approved fictional records and imaging.",
                                "assignment": {
                                    k: body[k]
                                    for k in (
                                        "patient_contact_id",
                                        "provider_contact_id",
                                        "requirements",
                                    )
                                },
                            }
                        ],
                    },
                )
                response.raise_for_status()
                deadline = time.monotonic() + 180
                sent_response = False
                while time.monotonic() < deadline:
                    await dispatch_once(database, temporal, queue, firm_id)
                    async with database.sessions() as session:
                        task = await session.scalar(
                            select(Task).where(
                                Task.tenant_id == firm_id,
                                Task.kind == "AGENT",
                            )
                        )
                        coord = await session.scalar(
                            select(CaseCoordination).where(
                                CaseCoordination.tenant_id == firm_id,
                                CaseCoordination.case_id == case_id,
                            )
                        )
                        if coord and coord.workflow_id not in workflow_ids:
                            workflow_ids.append(coord.workflow_id)
                        if task:
                            task_id = task.id
                            if task.workflow_id not in workflow_ids:
                                workflow_ids.append(task.workflow_id)
                            if task.status == "WAITING" and not sent_response:
                                reply = api.post(
                                    f"/v1/firms/{firm_id}/tasks/{task.id}/provider-updates",
                                    headers={**auth, "Idempotency-Key": "trace-demo-receipt"},
                                    json={
                                        "status": "RECEIVED",
                                        "message": "Fictional records received.",
                                    },
                                )
                                reply.raise_for_status()
                                sent_response = True
                            if task.status == "COMPLETED" and coord.status == "SATISFIED":
                                break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Trace demo did not complete within three minutes")
                async with database.sessions() as session:
                    runs = (
                        await session.scalars(
                            select(Execution)
                            .where(
                                Execution.task_id == task_id,
                            )
                            .order_by(Execution.cycle)
                        )
                    ).all()
                    result = {
                        "mode": settings.agent_mode,
                        "status": "COMPLETED",
                        "executions": [
                            {
                                "execution_id": str(run.id),
                                **run.context_json.get("observability", {}),
                            }
                            for run in runs
                        ],
                    }
                await asyncio.to_thread(telemetry.flush)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, indent=2) + "\n")
                print(json.dumps(result, indent=2))
                for identity in workflow_ids:
                    try:
                        await temporal.get_workflow_handle(identity).terminate(
                            "Trace demo complete"
                        )
                    except Exception:
                        pass  # The specialist already completed; its workflow may be closed.
    finally:
        for identity in workflow_ids:
            try:
                await temporal.get_workflow_handle(identity).terminate("Trace demo cleanup")
            except Exception:
                pass
        await database.close()
        telemetry.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Make real model API calls (incurs usage)"
    )
    args = parser.parse_args()
    load_dotenv()
    settings = Settings(agent_mode="openai" if args.live else "scripted", otel_enabled=True)
    admin_url = make_url(settings.migration_database_url or settings.database_url)
    admin_connection = admin_url.set(drivername="postgresql").render_as_string(hide_password=False)
    name = "agents_trace_" + uuid4().hex[:16]
    with psycopg.connect(admin_connection, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            url = admin_url.set(database=name).render_as_string(hide_password=False)
            migration = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                env={**os.environ, "DATABASE_URL": url},
                capture_output=True,
                text=True,
            )
            if migration.returncode:
                raise RuntimeError("Could not migrate disposable trace database")
            asyncio.run(
                journey(
                    settings.model_copy(update={"database_url": url}),
                    Path("work/observability/latest-demo.json"),
                )
            )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


if __name__ == "__main__":
    main()

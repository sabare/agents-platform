"""Local fictional demo: saved send, injected failure, manual call, recover and complete."""

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from agents.tool_context import ToolContext
from dotenv import load_dotenv
from sqlalchemy import func, select

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.execution import TaskActivities
from agents_platform.models import Firm, OutboxMessage, ToolInvocation
from agents_platform.seed import seed_demo
from agents_platform.task_contracts import CreateRecordsTask
from agents_platform.tasks import create_records_task_in_transaction


async def inject_failure():
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            fixture = await seed_demo(session, uuid4())
        async with database.sessions.begin() as session:
            firm = await session.get(Firm, UUID(fixture["firm_id"]))
            firm.name = "Recovery and contact demo " + fixture["firm_id"][:8]
            task, _ = await create_records_task_in_transaction(
                session,
                UUID(fixture["firm_id"]),
                CreateRecordsTask(**{**fixture["assignment"], "agent_version": 3}),
                "recovery-demo",
            )
            # Keep this fictional task off the shared dispatch queue until the controlled
            # failure has been recorded. Recovery creates a new immediately due dispatch.
            for message in (
                await session.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.tenant_id == task.tenant_id,
                        OutboxMessage.status == "PENDING",
                    )
                )
            ).all():
                message.available_at = datetime.now(timezone.utc) + timedelta(days=1)
        worker = TaskActivities(database, settings)
        activation = {
            "tenant_id": str(task.tenant_id),
            "task_id": str(task.id),
            "cycle": 0,
            "trigger": "created",
        }
        run = await worker._prepare(activation)
        tool = next(t for t in worker._tools(run) if t.name == "send_records_request")
        await tool.on_invoke_tool(
            ToolContext(
                context=None, tool_name=tool.name, tool_call_id="demo", tool_arguments="{}"
            ),
            "{}",
        )
        await worker.fail_task({**activation, "failure_type": "SimulatedPostSendFailure"})
        return fixture, str(task.id)
    finally:
        await database.close()


def checked(response):
    response.raise_for_status()
    return response.json()


def wait(api, path, predicate):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        value = checked(api.get(path))
        if predicate(value):
            return value
        time.sleep(0.5)
    raise RuntimeError("Demo did not reach the expected state: " + path)


async def saved_sends(task_id):
    database = Database(get_settings().database_url)
    try:
        async with database.sessions() as session:
            return await session.scalar(
                select(func.count())
                .select_from(ToolInvocation)
                .where(
                    ToolInvocation.task_id == UUID(task_id),
                    ToolInvocation.logical_key == "initial-send",
                )
            )
    finally:
        await database.close()


def main():
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leave-failed", action="store_true", help="Leave an incident for dashboard review"
    )
    args = parser.parse_args()
    fixture, task_id = asyncio.run(inject_failure())
    firm = fixture["firm_id"]
    root = f"/v1/firms/{firm}"
    path = root + f"/tasks/{task_id}"
    dashboard = f"http://localhost:8000/?firm={firm}&task={task_id}"
    headers = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=20) as api:
        incident = checked(api.get(root + "/incidents", params={"task_id": task_id}))["items"][0]
        print(
            json.dumps(
                {
                    "phase": "FAILED",
                    "incident_id": incident["id"],
                    "saved_initial_sends": asyncio.run(saved_sends(task_id)),
                    "dashboard": dashboard,
                },
                indent=2,
            ),
            flush=True,
        )
        if args.leave_failed:
            return
        followup = datetime.now(timezone.utc) + timedelta(seconds=3)
        body = {
            "expected_task_version": incident["task_version"],
            "expected_incident_version": incident["version"],
            "action": "RETRY",
            "reason": "Injected demo failure cleared; reviewed saved effects and provider call.",
            "contact": {
                "contact_id": fixture["assignment"]["provider_contact_id"],
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "channel": "PHONE",
                "outcome": "REACHED",
                "notes": "Demo case manager called provider; remaining items promised shortly.",
                "follow_up_at": followup.isoformat(),
                "response_due_at": (followup + timedelta(minutes=5)).isoformat(),
            },
        }
        decision_path = root + f"/incidents/{incident['id']}/resolve"
        checked(api.post(decision_path, headers={"Idempotency-Key": "resume"}, json=body))
        assert checked(api.post(decision_path, headers={"Idempotency-Key": "resume"}, json=body))[
            "replayed"
        ]
        wait(
            api,
            path,
            lambda t: any(
                e["event_type"] == "records.human_follow_up_sent"
                for e in checked(api.get(path + "/events"))
            ),
        )
        checked(
            api.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "received"},
                json={
                    "status": "RECEIVED",
                    "message": "Synthetic records delivered after manual call.",
                },
            )
        )
        task = wait(api, path, lambda t: t["status"] in ("COMPLETED", "FAILED"))
        assert task["status"] == "COMPLETED"
        assert asyncio.run(saved_sends(task_id)) == 1
        print(
            json.dumps(
                {
                    "phase": task["status"],
                    "task_id": task_id,
                    "saved_initial_sends": 1,
                    "workflow_id": task["workflow_id"],
                    "dashboard": dashboard,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

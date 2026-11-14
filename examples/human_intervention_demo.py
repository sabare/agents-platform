"""Three automatic reminders, one human-authorized follow-up, same-task completion."""

import argparse
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
from check_in_demo import checked, fixture
from dotenv import load_dotenv

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import Firm


async def create_fixture():
    data = await fixture()
    database = Database(get_settings().database_url)
    try:
        async with database.sessions.begin() as session:
            firm = await session.get(Firm, UUID(data["firm_id"]))
            firm.name = "Human intervention demo " + data["firm_id"][:8]
    finally:
        await database.close()
    return data


def wait(api, path, predicate, label):
    # Real-model tool turns add latency beyond the deliberately short demo cadence.
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current = checked(api.get(path))
        if predicate(current):
            return current
        if current.get("status") == "FAILED":
            raise RuntimeError("Task failed: " + label)
        time.sleep(0.5)
    raise RuntimeError("Timed out: " + label)


def main():
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firm", help="Resume an existing demo firm")
    parser.add_argument("--task", help="Resume its existing records task")
    args = parser.parse_args()
    if bool(args.firm) != bool(args.task):
        parser.error("Supply both --firm and --task to resume")
    data = None if args.firm else asyncio.run(create_fixture())
    firm = args.firm or data["firm_id"]
    headers = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    root = f"/v1/firms/{firm}"
    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=20) as api:
        if args.task:
            task = checked(api.get(root + f"/tasks/{args.task}"))
        else:
            task = checked(
                api.post(
                    root + "/tasks/medical-records",
                    headers={"Idempotency-Key": "records"},
                    json={**data["assignment"], "agent_version": 3},
                )
            )
        path = root + f"/tasks/{task['id']}"
        print("Waiting for the bounded automatic reminder policy to exhaust…", flush=True)
        wait(api, path, lambda t: t["status"] == "BLOCKED", "records intervention")
        request = checked(api.get(root + "/human-requests", params={"task_id": task["id"]}))[
            "items"
        ][0]
        at = datetime.now(timezone.utc) + timedelta(seconds=3)
        body = {
            "expected_task_version": request["task_version"],
            "expected_request_version": request["version"],
            "action": "SCHEDULE_FOLLOW_UP",
            "reason": "Demo operator reviewed the history and authorized one additional attempt.",
            "payload": {
                "follow_up_at": at.isoformat(),
                "response_due_at": (at + timedelta(minutes=5)).isoformat(),
            },
        }
        decision_path = root + f"/human-requests/{request['id']}/decisions"
        checked(api.post(decision_path, headers={"Idempotency-Key": "one-extra"}, json=body))
        assert checked(
            api.post(decision_path, headers={"Idempotency-Key": "one-extra"}, json=body)
        )["replayed"]
        print("Decision saved twice with one key; exactly one follow-up authorized.", flush=True)
        wait(
            api,
            root + f"/human-requests/{request['id']}",
            lambda r: bool(r["continuations"] and r["continuations"][0]["status"] == "APPLIED"),
            "human-authorized follow-up",
        )
        checked(
            api.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "received"},
                json={
                    "status": "RECEIVED",
                    "message": "Synthetic records delivered after the authorized follow-up.",
                },
            )
        )
        result = wait(api, path, lambda t: t["status"] == "COMPLETED", "original task completes")
        assert result["workflow_id"] == task["workflow_id"] and result["agent_version"] == 3
        print(
            json.dumps(
                {
                    "task_id": task["id"],
                    "status": result["status"],
                    "human_request_id": request["id"],
                    "agent_version": 3,
                    "dashboard": f"http://localhost:8000/?firm={firm}&task={task['id']}",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

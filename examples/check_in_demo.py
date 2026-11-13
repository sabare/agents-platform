"""Two fictional client check-ins through real model, durable scheduling and comparison."""

import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from dotenv import load_dotenv

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import Firm
from agents_platform.seed import seed_demo


async def fixture():
    db = Database(get_settings().database_url)
    try:
        async with db.sessions() as session:
            data = await seed_demo(session, uuid4())
            async with session.begin():
                firm = await session.get(Firm, UUID(data["firm_id"]))
                firm.name = "Client check-in demo " + data["firm_id"][:8]
            return data
    finally:
        await db.close()


def checked(response):
    response.raise_for_status()
    return response.json()


def wait(api, path, predicate, label):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        current = checked(api.get(path))
        if predicate(current):
            return current
        if current.get("status") == "FAILED":
            raise RuntimeError("Task failed: " + label)
        time.sleep(0.5)
    raise RuntimeError("Timed out: " + label + " " + json.dumps(current))


def main():
    load_dotenv(".env")
    data = asyncio.run(fixture())
    firm, case, client = (
        data["firm_id"],
        data["assignment"]["case_id"],
        data["assignment"]["patient_contact_id"],
    )
    case_path = f"/v1/firms/{firm}/cases/{case}"
    auth = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=auth, timeout=20) as api:
        due = datetime.now(timezone.utc) + timedelta(seconds=8)
        first = checked(
            api.post(
                f"/v1/firms/{firm}/tasks/client-check-in",
                headers={"Idempotency-Key": "baseline"},
                json={
                    "case_id": case,
                    "client_contact_id": client,
                    "scheduled_at": due.isoformat(),
                    "objective": "Check in with the client and save an attributed baseline.",
                },
            )
        )
        path = f"/v1/firms/{firm}/tasks/{first['id']}"
        current = checked(api.get(path))
        assert current["client_check_in"]["sent_at"] is None
        subprocess.run(["docker", "compose", "stop", "worker"], check=True, capture_output=True)
        try:
            assert checked(api.get(path))["client_check_in"]["sent_at"] is None
        finally:
            subprocess.run(
                ["docker", "compose", "start", "worker"], check=True, capture_output=True
            )
        wait(api, path, lambda t: bool(t["client_check_in"]["sent_at"]), "scheduled send")
        baseline = checked(
            api.post(
                path + "/client-replies",
                headers={"Idempotency-Key": "baseline-reply"},
                json={
                    "message": "Fictional client: doing about the same, attending treatment.",
                    "pain_score": 3,
                    "daily_function": "UNCHANGED",
                },
            )
        )
        wait(api, path, lambda t: t["status"] == "COMPLETED", "baseline completion")
        second = checked(
            api.post(
                f"/v1/firms/{firm}/tasks/client-check-in",
                headers={"Idempotency-Key": "follow-up"},
                json={
                    "case_id": case,
                    "client_contact_id": client,
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                    "objective": "Compare this client report with the earlier baseline.",
                },
            )
        )
        second_path = f"/v1/firms/{firm}/tasks/{second['id']}"
        wait(api, second_path, lambda t: bool(t["client_check_in"]["sent_at"]), "follow-up send")
        changed = checked(
            api.post(
                second_path + "/client-replies",
                headers={"Idempotency-Key": "changed-reply"},
                json={
                    "message": (
                        "Fictional client: daily activities are harder "
                        "and I would like a teammate to call."
                    ),
                    "pain_score": 6,
                    "daily_function": "WORSE",
                    "requests_help": True,
                },
            )
        )
        wait(api, second_path, lambda t: t["status"] == "BLOCKED", "sensitive reply review")
        supervisor = wait(
            api,
            case_path + "/supervisor",
            lambda s: any(
                u["report_id"] == changed["report"]["id"]
                for u in s["plan"].get("client_updates", [])
            ),
            "meaningful update delivered to supervisor",
        )
        assert supervisor["status"] == "NEEDS_REVIEW"
        assert changed["report"]["previous_report_id"] == baseline["report"]["id"]
        assert changed["report"]["changes"]["pain_delta"] == 3
        sends = []
        for task_path in (path, second_path):
            runs = checked(api.get(task_path + "/executions"))
            count = sum(t["name"] == "send_client_check_in" for r in runs for t in r["tools"])
            assert count == 1
            sends.append(count)
        print(
            json.dumps(
                {
                    "firm_id": firm,
                    "case_id": case,
                    "baseline_task_id": first["id"],
                    "changed_task_id": second["id"],
                    "supervisor_group_id": supervisor["group_task_id"],
                    "supervisor_status": supervisor["status"],
                    "pain_delta": 3,
                    "worker_restart": True,
                    "sends_per_task": sends,
                    "url": f"http://localhost:8000/?firm={firm}&task={second['id']}",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

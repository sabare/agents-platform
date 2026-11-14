"""A timer causes a visible simulated provider reminder, then synthetic records finish the task."""

import asyncio
import json

import httpx
from check_in_demo import checked, fixture, wait
from dotenv import load_dotenv

from agents_platform.config import get_settings


def main():
    load_dotenv(".env")
    data = asyncio.run(fixture())
    firm = data["firm_id"]
    auth = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=auth, timeout=20) as api:
        task = checked(
            api.post(
                f"/v1/firms/{firm}/tasks/medical-records",
                headers={"Idempotency-Key": "reminder-demo"},
                json={**data["assignment"], "agent_version": 3},
            )
        )
        path = f"/v1/firms/{firm}/tasks/{task['id']}"
        wait(api, path, lambda t: t["status"] == "WAITING", "initial simulated request")
        print("Initial request saved. Waiting for the 30-second reminder policy…", flush=True)
        wait(
            api,
            path,
            lambda t: t["records_request"]["reminder_count"] >= 1,
            "visible simulated reminder",
        )
        checked(
            api.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "received"},
                json={
                    "status": "RECEIVED",
                    "message": "Fictional provider update: synthetic records are ready.",
                },
            )
        )
        complete = wait(
            api, path, lambda t: t["status"] == "COMPLETED", "received evidence completes task"
        )
        events = checked(api.get(path + "/events"))
        reminders = [e for e in events if e["event_type"] == "records.reminder_sent"]
        assert reminders and len(reminders) == complete["records_request"]["reminder_count"]
        assert complete["records_request"]["next_reminder_at"] is None
        print(
            json.dumps(
                {
                    "firm_id": firm,
                    "task_id": task["id"],
                    "status": complete["status"],
                    "reminders": len(reminders),
                    "agent_version": complete["agent_version"],
                    "mode": get_settings().agent_mode,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

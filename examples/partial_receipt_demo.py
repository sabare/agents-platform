"""Bills arrive first; the durable agent follows up only for records and imaging."""

import argparse
import asyncio
import json
import time
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
            firm.name = "Partial records demo " + data["firm_id"][:8]
    finally:
        await database.close()
    return data


def wait(api, path, predicate, label):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        value = checked(api.get(path))
        if predicate(value):
            return value
        if value.get("status") == "FAILED":
            raise RuntimeError("Demo task failed: " + label)
        time.sleep(0.5)
    raise RuntimeError("Timed out: " + label)


def main():
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leave-partial",
        action="store_true",
        help="Leave records/imaging outstanding for dashboard inspection",
    )
    args = parser.parse_args()
    data = asyncio.run(create_fixture())
    firm = data["firm_id"]
    headers = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    root = f"/v1/firms/{firm}"
    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=20) as api:
        task = checked(
            api.post(
                root + "/tasks/medical-records",
                headers={"Idempotency-Key": "partial-demo"},
                json={
                    **data["assignment"],
                    "agent_version": 3,
                    "requirements": [
                        {"record_type": kind, "all_dates": True}
                        for kind in ("RECORDS", "BILLS", "IMAGING")
                    ],
                },
            )
        )
        path = root + f"/tasks/{task['id']}"
        wait(api, path, lambda t: t["records_request"]["status"] == "SENT", "initial request")
        ids = {r["record_type"]: r["id"] for r in task["records_request"]["requirements"]}
        delivery = {
            "status": "PARTIAL",
            "message": "Bills delivered. Records and imaging are still being processed.",
            "received_requirement_ids": [ids["BILLS"]],
        }
        checked(
            api.post(
                path + "/provider-updates", headers={"Idempotency-Key": "bills"}, json=delivery
            )
        )
        assert checked(
            api.post(
                path + "/provider-updates", headers={"Idempotency-Key": "bills"}, json=delivery
            )
        )["replayed"]
        partial = checked(api.get(path))
        assert partial["records_request"]["status"] == "PARTIAL"
        print(
            "Bills received; waiting for a reminder scoped to the missing records and imaging…",
            flush=True,
        )

        def targeted(events):
            return any(
                e["event_type"] == "records.reminder_sent"
                and set(e["payload"].get("requested_requirement_ids", []))
                == {ids["RECORDS"], ids["IMAGING"]}
                for e in events
            )

        # Event routes return a list; wait on the task and inspect its saved event history.
        wait(api, path, lambda t: targeted(checked(api.get(path + "/events"))), "targeted reminder")
        events = checked(api.get(path + "/events"))
        follow_up = next(
            e["payload"]
            for e in events
            if e["event_type"] == "records.reminder_sent"
            and set(e["payload"]["requested_requirement_ids"]) == {ids["RECORDS"], ids["IMAGING"]}
        )
        assert "bills" not in follow_up["message"]
        print(
            json.dumps(
                {
                    "phase": "PARTIAL",
                    "follow_up_message": follow_up["message"],
                    "dashboard": f"http://localhost:8000/?firm={firm}&task={task['id']}",
                },
                indent=2,
            ),
            flush=True,
        )
        if args.leave_partial:
            return
        checked(
            api.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "remaining"},
                json={
                    "status": "RECEIVED",
                    "message": "Remaining records and imaging delivered.",
                    "received_requirement_ids": [ids["RECORDS"], ids["IMAGING"]],
                },
            )
        )
        final = wait(api, path, lambda t: t["status"] == "COMPLETED", "evidence-backed completion")
        assert final["workflow_id"] == task["workflow_id"]
        print(
            json.dumps(
                {
                    "phase": final["status"],
                    "task_id": task["id"],
                    "received_items": [
                        r["record_type"] for r in final["records_request"]["requirements"]
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

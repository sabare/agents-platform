"""Create a fictional case and demonstrate supervisor → specialist → supervisor."""

import argparse
import asyncio
import json
import subprocess
import time
from uuid import UUID, uuid4

import httpx
from dotenv import load_dotenv

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import Firm
from agents_platform.seed import seed_demo


async def fixture():
    database = Database(get_settings().database_url)
    try:
        async with database.sessions() as session:
            data = await seed_demo(session, uuid4())
            async with session.begin():
                firm = await session.get(Firm, UUID(data["firm_id"]))
                firm.name = "Supervisor demo " + data["firm_id"][:8]
            return data
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart-worker", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    data = asyncio.run(fixture())
    assignment = data["assignment"]
    firm, case = data["firm_id"], assignment["case_id"]
    headers = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=15) as api:
        path = f"/v1/firms/{firm}/cases/{case}/supervisor"
        body = {
            "objective": "Collect the client's records and imaging from Example Hospital.",
            "targets": [
                {
                    key: assignment[key]
                    for key in ("patient_contact_id", "provider_contact_id", "requirements")
                }
            ],
        }
        response = api.post(path, json=body, headers={"Idempotency-Key": "supervisor-demo"})
        response.raise_for_status()
        replay = api.post(path, json=body, headers={"Idempotency-Key": "supervisor-demo"})
        replay.raise_for_status()
        assert replay.json()["replayed"]
        deadline, task = time.monotonic() + 120, None
        while time.monotonic() < deadline:
            status = api.get(path).json()
            if status["status"] in ("FAILED", "NEEDS_REVIEW"):
                raise RuntimeError(json.dumps(status["plan"]))
            rows = api.get(f"/v1/operator/firms/{firm}/tasks").json()["items"]
            if rows:
                assert len(rows) == 1, "Supervisor duplicated an assignment"
                task = rows[0]
                if task["status"] == "WAITING":
                    break
            time.sleep(1)
        else:
            raise RuntimeError("Delegated specialist did not reach WAITING")
        stopped = False
        try:
            if args.restart_worker:
                subprocess.run(
                    ["docker", "compose", "stop", "worker"], check=True, capture_output=True
                )
                stopped = True
            response = api.post(
                f"/v1/firms/{firm}/tasks/{task['id']}/provider-updates",
                json={
                    "status": "RECEIVED",
                    "message": "Simulated records and imaging ready while worker is offline.",
                },
                headers={"Idempotency-Key": "records-received"},
            )
            response.raise_for_status()
        finally:
            if stopped:
                subprocess.run(
                    ["docker", "compose", "start", "worker"], check=True, capture_output=True
                )
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            current = api.get(path)
            current.raise_for_status()
            state = current.json()
            if state["status"] == "SATISFIED":
                break
            if state["status"] in ("FAILED", "NEEDS_REVIEW"):
                raise RuntimeError(json.dumps(state["plan"]))
            time.sleep(1)
        else:
            raise RuntimeError("Supervisor did not verify completion")
        worker_runs = api.get(f"/v1/firms/{firm}/tasks/{task['id']}/executions").json()
        supervisor_runs = api.get(
            f"/v1/firms/{firm}/tasks/{state['group_task_id']}/executions"
        ).json()
        sends = sum(t["name"] == "send_records_request" for r in worker_runs for t in r["tools"])
        assert sends == 1
        print(
            json.dumps(
                {
                    "firm_id": firm,
                    "case_id": case,
                    "specialist_task_id": task["id"],
                    "supervisor_group_id": state["group_task_id"],
                    "status": state["status"],
                    "supervisor_executions": len(supervisor_runs),
                    "specialist_executions": len(worker_runs),
                    "model_modes": sorted({r["mode"] for r in supervisor_runs + worker_runs}),
                    "initial_requests": sends,
                    "worker_restart": args.restart_worker,
                    "summary": state["plan"]["summary"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

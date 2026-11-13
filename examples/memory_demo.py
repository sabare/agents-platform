"""Real-model memory smoke: persisted case hold, hybrid search, release, completion."""

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
from agents_platform.models import Firm, Source
from agents_platform.seed import seed_demo


async def fixture():
    database = Database(get_settings().database_url)
    try:
        async with database.sessions() as session:
            data = await seed_demo(session, uuid4())
            async with session.begin():
                firm = await session.get(Firm, UUID(data["firm_id"]))
                firm.name = "Full memory demo " + data["firm_id"][:8]
                source = Source(
                    id=uuid4(),
                    tenant_id=firm.id,
                    case_id=UUID(data["assignment"]["case_id"]),
                    source_key="demo/old-authorization",
                    kind="DEMO_PROVIDER_MESSAGE",
                    created_at=datetime.now(timezone.utc) - timedelta(days=270),
                    content={
                        "message": (
                            "Fictional history: Example Hospital rejected the medical records "
                            "authorization because the client signature was missing. "
                            "The corrected form was later supplied."
                        )
                    },
                )
                session.add(source)
            return data, str(source.id)
    finally:
        await database.close()


def worker(command):
    subprocess.run(["docker", "compose", command, "worker"], check=True, capture_output=True)


def checked(response):
    response.raise_for_status()
    return response.json()


def wait(api, path, predicate, label):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        task = checked(api.get(path))
        if predicate(task):
            return task
        if task["status"] == "FAILED":
            raise RuntimeError("Task execution failed")
        time.sleep(1)
    raise RuntimeError(f"Timed out: {label}; latest={task}")


def memory_change(api, path, key, payload):
    for _ in range(4):
        task = checked(api.get(path))
        response = api.post(
            path + "/memory",
            headers={"Idempotency-Key": key},
            json={**payload, "expected_version": task["version"]},
        )
        if response.status_code != 409:
            return checked(response)
    raise RuntimeError("Memory write remained stale")


def main():
    load_dotenv(".env")
    data, historic_source = asyncio.run(fixture())
    firm, case = data["firm_id"], data["assignment"]["case_id"]
    headers = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    case_path = f"/v1/firms/{firm}/cases/{case}"
    worker("stop")
    try:
        with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=30) as api:
            hold = checked(
                api.post(
                    case_path + "/restrictions",
                    headers={"Idempotency-Key": "case-hold"},
                    json={
                        "expected_version": 0,
                        "text": "Fictional demo: pause all case contact "
                        "until permission is verified.",
                    },
                )
            )
            task = checked(
                api.post(
                    f"/v1/firms/{firm}/tasks/medical-records",
                    headers={"Idempotency-Key": "memory-task"},
                    json=data["assignment"],
                )
            )
            path = f"/v1/firms/{firm}/tasks/{task['id']}"
    finally:
        worker("start")

    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=30) as api:
        task = wait(api, path, lambda t: t["status"] == "BLOCKED", "case hold blocks future task")
        runs = checked(api.get(path + "/executions"))
        assert all(not r["tools"] and not r["usage"].get("requests", 0) for r in runs)
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            search = checked(
                api.get(
                    case_path + "/memory/search",
                    params={"q": "paperwork rejected missing signature"},
                )
            )
            if any(
                r["source_id"] == historic_source and "vector" in r["lanes"]
                for r in search["results"]
            ):
                break
            time.sleep(2)
        else:
            raise RuntimeError("Real vector index did not return historical evidence")
        assert checked(api.get(case_path + "/memory"))["summary"]
        checked(
            api.post(
                case_path + "/restrictions",
                headers={"Idempotency-Key": "case-release"},
                json={
                    "expected_version": 1,
                    "operation": "RESOLVE",
                    "item_id": hold["item_id"],
                    "text": "Fictional demo: teammate verified permission "
                    "and supplied corrected authorization.",
                },
            )
        )
        task = wait(
            api,
            path,
            lambda t: t["status"] == "WAITING" and t["records_request"]["status"] == "SENT",
            "real agent sends initial request",
        )
        obligation = memory_change(
            api,
            path,
            "verify-imaging",
            {
                "kind": "OBLIGATION",
                "text": "Fictional demo: teammate must verify imaging export before completion.",
            },
        )
        checked(
            api.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "documents"},
                json={"status": "RECEIVED", "message": "Fictional records and imaging ready."},
            )
        )
        task = wait(
            api,
            path,
            lambda t: (
                t["status"] == "BLOCKED"
                and "obligation" in t["progress_json"].get("last_reason", "").lower()
            ),
            "obligation prevents completion",
        )
        memory_change(
            api,
            path,
            "verified",
            {
                "operation": "RESOLVE",
                "item_id": obligation["item_id"],
                "text": "Fictional demo: imaging export verified.",
            },
        )
        task = wait(api, path, lambda t: t["status"] == "COMPLETED", "task completes with evidence")
        runs = checked(api.get(path + "/executions"))
        sends = sum(t["name"] == "send_records_request" for r in runs for t in r["tools"])
        assert sends == 1
        result = {
            "firm_id": firm,
            "case_id": case,
            "task_id": task["id"],
            "historic_source_id": historic_source,
            "status": task["status"],
            "hybrid_search": search["method"],
            "real_vector_hit": True,
            "case_hold_survived_restart": True,
            "obligation_blocked_completion": True,
            "initial_requests": sends,
            "rejected_plans": sum(
                r["outcome"].get("status") == "CONTINUE"
                and "DRAFT" in r["outcome"].get("reason", "")
                for r in runs
            ),
            "model_modes": sorted({r["mode"] for r in runs}),
            "url": f"http://localhost:8000/?firm={firm}&task={task['id']}",
        }

        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

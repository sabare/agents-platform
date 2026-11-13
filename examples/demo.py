"""Run the medical-records demo against the local stack; all provider data is simulated.

Usage: .venv/bin/python examples/demo.py [--restart-worker]
"""

import argparse
import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

from agents_platform.config import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-worker", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    headers = {"Authorization": "Bearer " + settings.operator_api_key.get_secret_value()}
    assignment = json.loads(Path(__file__).with_name("medical-records-task.json").read_text())
    firm = "00000000-0000-4000-8000-000000000001"
    with httpx.Client(base_url="http://localhost:8000", headers=headers, timeout=15) as client:
        response = client.post(
            f"/v1/firms/{firm}/tasks/medical-records",
            json=assignment,
            headers={"Idempotency-Key": "demo-" + str(uuid4())},
        )
        response.raise_for_status()
        path = response.headers["Location"]
        task_id = response.json()["id"]
        print("Created task:", task_id, flush=True)

        def wait_for(target):
            deadline, last = time.monotonic() + 120, None
            while time.monotonic() < deadline:
                current = client.get(path)
                current.raise_for_status()
                status = current.json()["status"]
                if status != last:
                    print("Task:", status, flush=True)
                    last = status
                if status == target:
                    return
                if status in ("FAILED", "CANCELLED", "BLOCKED"):
                    raise RuntimeError(f"Task stopped: {current.json()['progress_json']}")
                time.sleep(0.25)
            raise TimeoutError(f"Task did not reach {target}; inspect /executions")

        wait_for("WAITING")
        stopped = False
        try:
            if args.restart_worker:
                subprocess.run(["docker", "compose", "stop", "worker"], check=True)
                stopped = True
                print("Worker stopped; task state and wait remain saved.", flush=True)
            response = client.post(
                path + "/provider-updates",
                headers={"Idempotency-Key": "records-ready"},
                json={
                    "status": "RECEIVED",
                    "message": "Simulated provider response: records and imaging are ready.",
                },
            )
            response.raise_for_status()
            print("Saved provider response and simulated record evidence.", flush=True)
        finally:
            if stopped:
                subprocess.run(["docker", "compose", "start", "worker"], check=True)
        wait_for("COMPLETED")
        runs = client.get(path + "/executions")
        runs.raise_for_status()
        history = runs.json()
        summary = {
            "task_id": task_id,
            "status": "COMPLETED",
            "executions": len(history),
            "mode": history[-1]["mode"],
            "model_requests": sum((r["usage"] or {}).get("requests", 0) for r in history),
            "total_tokens": sum((r["usage"] or {}).get("total_tokens", 0) for r in history),
            "execution_history": path + "/executions",
        }
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

"""Two real-timed occurrences, pause/review/resume, and a bounded finish; outreach is simulated."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
from check_in_demo import checked, fixture, wait
from dotenv import load_dotenv

from agents_platform.config import get_settings


def main():
    load_dotenv(".env")
    data = asyncio.run(fixture())
    firm, case = data["firm_id"], data["assignment"]["case_id"]
    auth = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=auth, timeout=20) as api:
        series = checked(
            api.post(
                f"/v1/firms/{firm}/cases/{case}/check-in-series",
                headers={"Idempotency-Key": "recurring-demo"},
                json={
                    "client_contact_id": data["assignment"]["patient_contact_id"],
                    "interval_seconds": 60,
                    "max_occurrences": 2,
                    "first_due_at": datetime.now(timezone.utc).isoformat(),
                    "ends_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                    "objective": "Two periodic simulated client check-ins "
                    "with a human review between them.",
                },
            )
        )
        group_path = f"/v1/firms/{firm}/tasks/{series['group_task_id']}"
        group = wait(
            api,
            group_path,
            lambda t: bool(t["check_in_series"]["active_task_id"]),
            "first occurrence",
        )
        first_path = f"/v1/firms/{firm}/tasks/{group['check_in_series']['active_task_id']}"
        wait(
            api, first_path, lambda t: bool(t["client_check_in"]["sent_at"]), "first simulated send"
        )
        checked(
            api.post(
                first_path + "/client-replies",
                headers={"Idempotency-Key": "first-reply"},
                json={
                    "message": "Fictional report: daily activity is worse "
                    "and I would like a callback.",
                    "pain_score": 9,
                    "daily_function": "WORSE",
                    "requests_help": True,
                },
            )
        )
        first = wait(api, first_path, lambda t: t["status"] == "BLOCKED", "first report review")
        group = checked(api.get(group_path))
        saved = group["check_in_series"]
        control_path = f"/v1/firms/{firm}/cases/{case}/check-in-series/{series['id']}/control"
        checked(
            api.post(
                control_path,
                headers={"Idempotency-Key": "pause"},
                json={
                    "expected_version": saved["version"],
                    "action": "PAUSE",
                    "reason": "Pause future outreach while a teammate handles the report.",
                },
            )
        )
        # Control wakes an already blocked task, so read the current version before deciding.
        first = wait(
            api,
            first_path,
            lambda t: t["status"] == "BLOCKED" and t["version"] > first["version"],
            "paused review activation",
        )
        review = first["client_check_in"]["report"]["human_review"]
        checked(
            api.post(
                first_path + f"/client-reviews/{review['id']}/decisions",
                headers={"Idempotency-Key": "acknowledge"},
                json={
                    "expected_task_version": first["version"],
                    "expected_review_version": review["version"],
                    "decision": "ALLOW_COMPLETION",
                    "reason": "Demo teammate handled the callback. Acknowledge this report; "
                    "contact restrictions remain unchanged.",
                },
            )
        )
        wait(
            api,
            first_path,
            lambda t: t["status"] == "COMPLETED",
            "reviewed first occurrence completes",
        )
        paused = checked(api.get(group_path))["check_in_series"]
        assert paused["status"] == "PAUSED" and paused["occurrence_count"] == 1
        checked(
            api.post(
                control_path,
                headers={"Idempotency-Key": "resume"},
                json={
                    "expected_version": paused["version"],
                    "action": "RESUME",
                    "reason": "Resume the approved interval after the first occurrence completes.",
                },
            )
        )
        print(
            "First occurrence reviewed and completed. Waiting for the real 60-second interval…",
            flush=True,
        )
        group = wait(
            api,
            group_path,
            lambda t: t["check_in_series"]["occurrence_count"] == 2,
            "second scheduled occurrence",
        )
        second_path = f"/v1/firms/{firm}/tasks/{group['check_in_series']['active_task_id']}"
        wait(
            api,
            second_path,
            lambda t: bool(t["client_check_in"]["sent_at"]),
            "second simulated send",
        )
        checked(
            api.post(
                second_path + "/client-replies",
                headers={"Idempotency-Key": "second-reply"},
                json={
                    "message": "Fictional report: I feel better today.",
                    "pain_score": 3,
                    "daily_function": "BETTER",
                },
            )
        )
        wait(api, second_path, lambda t: t["status"] == "COMPLETED", "second occurrence completes")
        group = wait(
            api,
            group_path,
            lambda t: t["check_in_series"]["status"] == "FINISHED",
            "series finishes at its bound",
        )
        occurrences = group["check_in_series"]["occurrences"]
        assert len(occurrences) == 2 and all(o["status"] == "COMPLETED" for o in occurrences)
        for occurrence in occurrences:
            runs = checked(api.get(f"/v1/firms/{firm}/tasks/{occurrence['task_id']}/executions"))
            assert sum(t["name"] == "send_client_check_in" for r in runs for t in r["tools"]) == 1
        print(
            json.dumps(
                {
                    "firm_id": firm,
                    "series_id": series["id"],
                    "group_task_id": series["group_task_id"],
                    "status": "FINISHED",
                    "occurrences": occurrences,
                    "mode": get_settings().agent_mode,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

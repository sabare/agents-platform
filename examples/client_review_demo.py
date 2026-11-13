"""Fictional client report -> guidance -> acknowledgement -> same-task completion."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
from check_in_demo import checked, fixture, wait
from dotenv import load_dotenv

from agents_platform.config import get_settings


def main():
    load_dotenv(".env")
    data = asyncio.run(fixture())
    firm = data["firm_id"]
    case = data["assignment"]["case_id"]
    auth = {"Authorization": "Bearer " + get_settings().operator_api_key.get_secret_value()}
    with httpx.Client(base_url="http://localhost:8000", headers=auth, timeout=20) as api:
        task = checked(
            api.post(
                f"/v1/firms/{firm}/tasks/client-check-in",
                headers={"Idempotency-Key": "human-review-demo"},
                json={
                    "case_id": case,
                    "client_contact_id": data["assignment"]["patient_contact_id"],
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        )
        path = f"/v1/firms/{firm}/tasks/{task['id']}"
        wait(api, path, lambda t: bool(t["client_check_in"]["sent_at"]), "simulated send")
        checked(
            api.post(
                path + "/client-replies",
                headers={"Idempotency-Key": "reply"},
                json={
                    "message": "Fictional report: daily activity is worse; please call me.",
                    "pain_score": 9,
                    "daily_function": "WORSE",
                    "requests_help": True,
                },
            )
        )
        blocked = wait(api, path, lambda t: t["status"] == "BLOCKED", "teammate review")
        original_workflow = blocked["workflow_id"]
        review = blocked["client_check_in"]["report"]["human_review"]
        decision_path = path + f"/client-reviews/{review['id']}/decisions"

        def decide(current, choice, reason, key):
            return checked(
                api.post(
                    decision_path,
                    headers={"Idempotency-Key": key},
                    json={
                        "expected_task_version": current["version"],
                        "expected_review_version": current["client_check_in"]["report"][
                            "human_review"
                        ]["version"],
                        "decision": choice,
                        "reason": reason,
                    },
                )
            )

        changes = decide(
            blocked,
            "REQUEST_CHANGES",
            "Keep this report open while I arrange a teammate callback. Summarize the concern.",
            "request-changes",
        )
        blocked = wait(
            api,
            path,
            lambda t: t["status"] == "BLOCKED" and t["version"] > changes["task_version"],
            "guidance processed without closing review",
        )
        acknowledged = decide(
            blocked,
            "ALLOW_COMPLETION",
            "Demo teammate reviewed the report and handled the callback. "
            "This check-in can finish; contact permission is unchanged.",
            "acknowledge",
        )
        complete = wait(api, path, lambda t: t["status"] == "COMPLETED", "same-task completion")
        assert complete["workflow_id"] == original_workflow
        assert complete["client_check_in"]["report"]["flagged_for_review"]
        supervisor_path = f"/v1/firms/{firm}/cases/{case}/supervisor"
        supervisor = wait(
            api,
            supervisor_path,
            lambda c: any(
                r["task_id"] == task["id"] and r["task_status"] == "COMPLETED"
                for r in c["plan"].get("client_review_outcomes", [])
            ),
            "supervisor receives final outcome",
        )
        runs = checked(api.get(path + "/executions"))
        sends = [
            tool
            for run in runs
            for tool in run.get("tools", [])
            if tool.get("name") == "send_client_check_in"
        ]
        assert len(sends) == 1, "Review must not send another client message"
        print(
            json.dumps(
                {
                    "firm_id": firm,
                    "task_id": task["id"],
                    "review_id": review["id"],
                    "workflow_id": original_workflow,
                    "status": complete["status"],
                    "decision_source_id": acknowledged["review"]["decision_source_id"],
                    "supervisor_status": supervisor["status"],
                    "mode": get_settings().agent_mode,
                    "simulated_send_invocations": len(sends),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

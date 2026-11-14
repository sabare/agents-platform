import asyncio
import json
import logging

import pytest

from agents_platform.operations import AlertTransitions
from agents_platform.telemetry import IDS, JsonFormatter, emit, log_context, observed_activity


def test_json_correlation_isolated_across_concurrent_tasks_and_no_payloads(caplog):
    async def task(firm):
        with log_context(firm_id=firm, task_id=f"task-{firm}"):
            await asyncio.sleep(0)
            emit("test")

    async def run():
        await asyncio.gather(task("A"), task("B"))

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    rows = [
        json.loads(JsonFormatter().format(r)) for r in caplog.records if r.getMessage() == "test"
    ]
    assert {r["firm_id"] for r in rows} == {"A", "B"}
    assert all(r["task_id"] == f"task-{r['firm_id']}" for r in rows)
    assert all(set(IDS) <= r.keys() for r in rows)
    record = logging.LogRecord("test", logging.INFO, "", 0, "after", (), None)
    assert json.loads(JsonFormatter().format(record))["firm_id"] is None


def test_alerts_only_log_open_and_clear_and_are_scoped_by_firm(caplog):
    tracker = AlertTransitions()

    def snap(firm, active, count=1):
        return {
            "firm_id": firm,
            "signals": [
                {
                    "key": "worker",
                    "alert": active,
                    "count": count,
                    "age_seconds": 200,
                    "items": [{"task_id": "task-A"}],
                }
            ],
        }

    with caplog.at_level(logging.INFO):
        tracker.update(snap("A", True))
        tracker.update(snap("A", True, 10))
        tracker.update(snap("B", False))
        tracker.update(snap("A", False))
        tracker.update(snap("A", False))
        tracker.update(snap("A", True))
    assert [r.getMessage() for r in caplog.records] == [
        "operations.alert_opened",
        "operations.alert_cleared",
        "operations.alert_opened",
    ]
    assert caplog.records[0].fields["task_id"] == "task-A"
    assert all(r.fields["firm_id"] == "A" for r in caplog.records)


async def test_activity_failures_log_type_not_sensitive_error_and_support_no_result(caplog):
    class Worker:
        @observed_activity
        async def run(self, activation):
            raise RuntimeError("secret-provider-message")

        @observed_activity
        async def finish(self, activation):
            return None

    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError):
            await Worker().run({"tenant_id": "firm-A", "task_id": "task-A"})
        assert await Worker().finish({"tenant_id": "firm-B"}) is None
    rows = [json.loads(JsonFormatter().format(r)) for r in caplog.records]
    assert "secret-provider-message" not in json.dumps(rows)
    failed = next(r for r in rows if r["message"] == "activity.failed")
    assert failed["error_type"] == "RuntimeError" and failed["firm_id"] == "firm-A"


def test_http_logs_use_generated_id_and_never_include_query_credentials(caplog):
    from fastapi.testclient import TestClient

    from agents_platform.api import create_app
    from agents_platform.config import Settings

    with caplog.at_level(logging.INFO, logger="agents_platform.telemetry"):
        with TestClient(create_app(Settings(_env_file=None))) as client:
            result = client.get(
                "/health/live?token=secret-value", headers={"X-Request-ID": "unsafe"}
            )
    records = [r for r in caplog.records if r.getMessage() == "http.finished"]
    row = json.loads(JsonFormatter().format(records[-1]))
    assert row["request_id"] == result.headers["X-Request-ID"] != "unsafe"
    assert row["route"] == "/health/live"
    assert "secret-value" not in json.dumps(row)

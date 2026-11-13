from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from agents_platform.api import create_app
from agents_platform.config import Settings
from agents_platform.orchestration.contracts import ProbeResult


@pytest.fixture
def api():
    app = create_app(Settings(_env_file=None))
    with TestClient(app) as client:
        yield app, client


def fake_temporal():
    return SimpleNamespace(
        workflow_service=SimpleNamespace(describe_namespace=AsyncMock()),
        start_workflow=AsyncMock(),
        get_workflow_handle=Mock(),
    )


def test_liveness_and_docs_work_without_external_services(api):
    _, client = api
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/openapi.json").status_code == 200


def test_readiness_checks_both_services(api):
    app, client = api
    app.state.database.check = AsyncMock(return_value="0.8.1")
    app.state.temporal = fake_temporal()
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": "ok", "temporal": "ok"}


def test_readiness_reports_outages_without_exposing_connection_details(api):
    app, client = api
    app.state.database.check = AsyncMock(side_effect=RuntimeError("private connection details"))
    app.state.temporal = fake_temporal()
    app.state.temporal.workflow_service.describe_namespace.side_effect = RuntimeError("unavailable")
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "unavailable", "temporal": "unavailable"}
    assert "private" not in response.text


def test_probe_is_queued_and_has_a_status_url(api):
    app, client = api
    app.state.temporal = fake_temporal()
    response = client.post("/dev/probes", json={"delay_seconds": 2})
    assert response.status_code == 202
    assert response.json()["status_url"].endswith(response.json()["probe_id"])
    call = app.state.temporal.start_workflow.call_args
    assert call.args[1].delay_seconds == 2
    assert call.kwargs["task_queue"] == "agents-platform"


def test_probe_does_not_report_success_when_temporal_is_unavailable(api):
    app, client = api
    app.state.temporal = fake_temporal()
    app.state.temporal.start_workflow.side_effect = RuntimeError("unavailable")
    assert client.post("/dev/probes", json={}).status_code == 503


def test_completed_probe_returns_worker_result(api):
    app, client = api
    app.state.temporal = fake_temporal()
    app.state.temporal.get_workflow_handle.return_value = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(status=WorkflowExecutionStatus.COMPLETED)),
        result=AsyncMock(return_value=ProbeResult(postgres="ok", pgvector_version="0.8.1")),
    )
    response = client.get(f"/dev/probes/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["result"]["postgres"] == "ok"


def test_unknown_probe_returns_404(api):
    app, client = api
    app.state.temporal = fake_temporal()
    app.state.temporal.get_workflow_handle.return_value = SimpleNamespace(
        describe=AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    )
    assert client.get(f"/dev/probes/{uuid4()}").status_code == 404


@pytest.mark.parametrize("seconds", [-1, 61])
def test_probe_rejects_invalid_timer_bounds(api, seconds):
    _, client = api
    assert client.post("/dev/probes", json={"delay_seconds": seconds}).status_code == 422

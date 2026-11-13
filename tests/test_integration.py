"""Run with `make smoke` after `make up`; exercises real services and worker."""

import os
import time

import httpx
import pytest


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Start the stack, then run make smoke"
)
def test_api_to_temporal_worker_to_postgres():
    with httpx.Client(base_url=os.getenv("API_URL", "http://localhost:8000"), timeout=10) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        response = client.post("/dev/probes", json={"delay_seconds": 1})
        assert response.status_code == 202, response.text
        status_url = response.json()["status_url"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.get(status_url)
            assert status.status_code == 200, status.text
            data = status.json()
            if data["status"] == "COMPLETED":
                assert data["result"]["postgres"] == "ok"
                assert data["result"]["pgvector_version"]
                return
            assert data["status"] == "RUNNING", data
            time.sleep(0.5)
        pytest.fail("Probe did not complete; verify that the worker is running")

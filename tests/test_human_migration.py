"""Upgrade preserves open/resolved client reviews and every earlier decision source."""

import os
import subprocess
import sys

import pytest
from test_client_reviews import blocked, decide
from test_memory import activation, runtime
from test_task_persistence import AUTH
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def migrate(database_url, direction, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


async def test_legacy_review_backfill_preserves_status_history_and_replay(setup, database_url):
    client, fixture, _ = setup
    database, worker = runtime(database_url)
    try:
        resolved = await blocked(client, fixture, worker, "resolved")
        assert decide(client, resolved).status_code == 200
        assert (await worker.execute_task(activation(fixture, resolved, 2)))[
            "status"
        ] == "COMPLETED"
        opened = await blocked(client, fixture, worker, "open")
        migrate(database_url, "downgrade", "ea47b89216dc")
        migrate(database_url, "upgrade", "head")
        response = client.get(
            f"/v1/firms/{fixture['firm_id']}/human-requests?status=ALL", headers=AUTH
        )
        assert response.status_code == 200, response.text
        rows = {r["task_id"]: r for r in response.json()["items"]}
        assert rows[resolved["id"]]["status"] == "RESOLVED"
        assert rows[opened["id"]]["status"] == "OPEN"
        history = rows[resolved["id"]]["decisions"]
        assert len(history) == 1 and history[0]["action"] == "ALLOW_COMPLETION"
        assert history[0]["actor"] == "local-operator"
        assert decide(client, resolved).json()["replayed"]
    finally:
        await database.close()

"""Upgrade preserves pre-lifecycle active/resolved memory and source identities."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Run make smoke"),
]


def migrate(database_url, direction, revision):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_memory_backfill_preserves_sources_and_separates_history(setup, database_url):
    client, fixture, _ = setup
    task = post(client, fixture).json()
    migrate(database_url, "downgrade", "2f5c65142b88")
    source_id, resolution_id, active_id, resolved_id = (uuid4() for _ in range(4))
    with connect(database_url) as connection:
        for source in (source_id, resolution_id):
            connection.execute(
                """INSERT INTO sources(id,tenant_id,case_id,source_key,kind,content)
                   VALUES (%s,%s,%s,%s,'OPERATOR_MEMORY_CHANGE',%s)""",
                (
                    source,
                    UUID(fixture["firm_id"]),
                    UUID(task["case_id"]),
                    str(source),
                    Jsonb({"text": "Original historical evidence"}),
                ),
            )
        items = [
            {
                "id": str(active_id),
                "kind": "STOP_CONTACT",
                "text": "Old active hold",
                "status": "OPEN",
                "revision": 1,
                "source_id": str(source_id),
                "due_at": None,
            },
            {
                "id": str(resolved_id),
                "kind": "OBLIGATION",
                "text": "Old resolved obligation",
                "status": "RESOLVED",
                "revision": 2,
                "source_id": str(source_id),
                "resolution_source_id": str(resolution_id),
                "resolution_reason": "Verified",
            },
        ]
        connection.execute(
            "UPDATE tasks SET progress_json=%s WHERE id=%s",
            (Jsonb({"memory_items": items, "unrelated": "keep"}), UUID(task["id"])),
        )
    migrate(database_url, "upgrade", "head")
    result = client.get(f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/memory", headers=AUTH)
    assert result.status_code == 200, result.text
    memory = result.json()
    assert [i["id"] for i in memory["operational_items"]] == [str(active_id)]
    history = {i["id"]: i for i in memory["history"]}
    assert history[str(resolved_id)]["resolution_source_id"] == str(resolution_id)
    assert history[str(resolved_id)]["closed_at"]
    with connect(database_url) as connection:
        progress = connection.execute(
            "SELECT progress_json FROM tasks WHERE id=%s", (UUID(task["id"]),)
        ).fetchone()[0]
        assert progress == {"unrelated": "keep"}
        assert connection.execute(
            "SELECT content FROM sources WHERE id=%s", (source_id,)
        ).fetchone()[0] == {"text": "Original historical evidence"}

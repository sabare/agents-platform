"""Distinguish the information source from the human recording it."""

import os
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from test_task_persistence import AUTH, connect, post
from test_task_persistence import database_url as database_url
from test_task_persistence import setup as setup

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION_TESTS") != "1", reason="Requires PostgreSQL"),
]


@pytest.mark.parametrize("legacy", [False, True])
def test_provider_feed_shows_hospital_and_keeps_recorder(setup, database_url, legacy):
    client, fixture, other = setup
    task = post(client, fixture).json()
    response = client.post(
        f"/v1/firms/{fixture['firm_id']}/tasks/{task['id']}/provider-updates",
        headers={**AUTH, "Idempotency-Key": "provider-status"},
        json={"status": "PENDING", "message": "Hospital is processing the request."},
    )
    assert response.status_code == 201, response.text
    if legacy:
        with connect(database_url) as c:
            event_id, payload = c.execute(
                "SELECT id,payload FROM activity_events WHERE task_id=%s "
                "AND event_type='provider.response_received'",
                (UUID(task["id"]),),
            ).fetchone()
            for key in ("provider_name", "provider_contact_id", "simulated"):
                payload.pop(key)
            c.execute(
                "UPDATE activity_events SET payload=%s WHERE id=%s", (Jsonb(payload), event_id)
            )
    url = f"/v1/operator/firms/{fixture['firm_id']}"
    assert client.get(url + "/tasks", headers=AUTH).status_code == 200
    feed = client.get(url + f"/activity?task_id={task['id']}", headers=AUTH)
    assert feed.status_code == 200, feed.text
    event = next(e for e in feed.json()["items"] if e["type"] == "provider.response_received")
    assert event["source_actor"]["id"] == fixture["assignment"]["provider_contact_id"]
    assert event["source_actor"]["id"] != other["assignment"]["provider_contact_id"]
    assert event["source_actor"]["name"] == "Example Hospital"
    assert event["recorded_by"] == event["actor"] == "local-operator"
    assert event["simulated"] is True

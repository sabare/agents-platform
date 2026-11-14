from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agents_platform.client_check_in import ClientCheckInAdapter
from agents_platform.human_request_handlers import ScheduleFollowUp
from agents_platform.human_requests import HumanResponse


def test_followup_authorization_requires_aware_bounded_dates_and_strict_payload():
    at = datetime.now(timezone.utc) + timedelta(days=1)
    valid = {"follow_up_at": at, "response_due_at": at + timedelta(days=1)}
    assert ScheduleFollowUp.model_validate(valid)
    for changes in (
        {"follow_up_at": at.replace(tzinfo=None)},
        {"follow_up_at": at - timedelta(days=2)},
        {"follow_up_at": at + timedelta(days=100)},
        {"response_due_at": at},
        {"response_due_at": at + timedelta(days=31)},
        {"clear_holds": True},
    ):
        with pytest.raises(ValidationError):
            ScheduleFollowUp.model_validate({**valid, **changes})
    with pytest.raises(ValidationError):
        HumanResponse(
            expected_task_version=1, expected_request_version=1, action="RECHECK", reason=" "
        )


async def test_monthly_suspension_without_interval_series_stays_a_review():
    async def view(session, task):
        return {
            "report": None,
            "series": None,
            "suspension_reason": "Monthly schedule paused",
            "sent_at": None,
        }

    adapter = ClientCheckInAdapter()
    decision = adapter.review("Monthly schedule paused")
    with patch("agents_platform.client_check_in.check_in_view", view):
        result = await adapter.validate(
            None,
            None,
            {"domain": {"state": {"check_in": {"report": None}}}},
            decision,
            [],
            decision.next_action,
        )
    assert result.action.type == "needs_human"
    assert result.action.reason == "Monthly schedule paused"


def test_manual_contact_requires_actual_time_and_explicit_reached_followup():
    from uuid import uuid4

    from agents_platform.recovery import ContactInput, RecoverTask

    at = datetime.now(timezone.utc)
    valid = {
        "contact_id": uuid4(),
        "occurred_at": at,
        "outcome": "REACHED",
        "notes": "Called provider",
    }
    assert ContactInput.model_validate(valid)
    for changes in (
        {"occurred_at": at + timedelta(hours=1)},
        {"occurred_at": at.replace(tzinfo=None)},
        {"notes": " "},
        {"follow_up_at": at + timedelta(days=1)},
        {
            "outcome": "VOICEMAIL",
            "follow_up_at": at + timedelta(days=1),
            "response_due_at": at + timedelta(days=2),
        },
    ):
        with pytest.raises(ValidationError):
            ContactInput.model_validate({**valid, **changes})
    with pytest.raises(ValidationError):
        RecoverTask(
            expected_task_version=1,
            expected_incident_version=1,
            action="CANCEL",
            reason="Stop",
            contact={
                **valid,
                "follow_up_at": at + timedelta(days=1),
                "response_due_at": at + timedelta(days=2),
            },
        )

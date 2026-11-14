from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agents_platform.monthly_check_ins import (
    CreateMonthly,
    calendar_due,
    first_due,
    following_month,
)


def test_calendar_clamps_end_of_month_without_drifting_to_the_28th():
    row = SimpleNamespace(day_of_month=31, local_time="09:00", timezone="UTC")
    january = calendar_due(2027, 1, 31, "09:00", "UTC")
    february = following_month(january, row)
    assert february == datetime(2027, 2, 28, 9, tzinfo=timezone.utc)
    assert following_month(february, row) == datetime(2027, 3, 31, 9, tzinfo=timezone.utc)
    assert calendar_due(2028, 2, 31, "09:00", "UTC").day == 29


def test_calendar_dst_uses_first_fold_and_shifts_nonexistent_time_forward():
    assert calendar_due(2027, 3, 14, "02:30", "America/New_York") == datetime(
        2027, 3, 14, 7, 30, tzinfo=timezone.utc
    )
    assert calendar_due(2027, 11, 7, "01:30", "America/New_York") == datetime(
        2027, 11, 7, 5, 30, tzinfo=timezone.utc
    )
    assert calendar_due(2027, 2, 5, "09:00", "Asia/Kolkata") == datetime(
        2027, 2, 5, 3, 30, tzinfo=timezone.utc
    )


def test_creation_and_resume_do_not_select_a_past_calendar_date():
    row = SimpleNamespace(day_of_month=5, local_time="09:00", timezone="UTC")
    assert first_due(row, datetime(2027, 1, 6, tzinfo=timezone.utc)) == datetime(
        2027, 2, 5, 9, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "Not/AZone"},
        {"local_time": "24:10"},
        {"day_of_month": 0},
        {"minimum_gap_days": 0},
        {"ends_at": "2027-03-01T09:00:00"},
        {"message": "  "},
    ],
)
def test_invalid_monthly_configuration_is_rejected(changes):
    with pytest.raises(ValidationError):
        CreateMonthly.model_validate(
            {
                "client_contact_id": "00000000-0000-0000-0000-000000000001",
                "timezone": "UTC",
                "day_of_month": 5,
                "local_time": "09:00",
                **changes,
            }
        )

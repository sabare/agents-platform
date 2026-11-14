from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agents_platform.provider_callbacks import resolve_callback
from agents_platform.task_contracts import ProviderUpdate

AT = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)  # Saturday


def update(message="Please call back Friday.", **fields):
    return ProviderUpdate(status="PENDING", message=message, **fields)


def test_weekday_uses_hospital_timezone_and_explicit_default_time():
    due, zone = resolve_callback(update(callback_timezone="Asia/Kolkata"), AT)
    assert due == datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)
    assert zone == "Asia/Kolkata"
    due, _ = resolve_callback(
        update("Please call back Friday at 2pm.", callback_timezone="America/New_York"), AT
    )
    assert due == datetime(2026, 11, 20, 19, tzinfo=timezone.utc)


def test_same_weekday_chooses_future_time_and_next_excludes_today():
    morning = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
    assert resolve_callback(update(callback_timezone="UTC"), morning)[0].day == 20
    assert resolve_callback(update(callback_timezone="UTC"), morning.replace(hour=10))[0].day == 27
    assert (
        resolve_callback(update("Call back next Friday", callback_timezone="UTC"), morning)[0].day
        == 27
    )


def test_tomorrow_resolves_in_provider_calendar_not_utc_calendar():
    at = datetime(2026, 11, 14, 2, tzinfo=timezone.utc)  # Still Friday in Los Angeles
    due, _ = resolve_callback(
        update("Call back tomorrow at 09:15", callback_timezone="America/Los_Angeles"), at
    )
    assert due == datetime(2026, 11, 14, 17, 15, tzinfo=timezone.utc)


def test_explicit_date_and_offset_are_supported():
    assert resolve_callback(
        update(
            "Date agreed by phone",
            callback_date="2026-11-16",
            callback_local_time="14:30",
            callback_timezone="Asia/Kolkata",
        ),
        AT,
    )[0] == datetime(2026, 11, 16, 9, tzinfo=timezone.utc)
    assert resolve_callback(update(callback_at="2026-11-16T14:30:00+05:30"), AT) == (
        datetime(2026, 11, 16, 9, tzinfo=timezone.utc),
        "UTC",
    )


@pytest.mark.parametrize(
    "message",
    [
        "Call back sometime next week",
        "Call back Friday afternoon",
        "Call back Friday at 2",
        "Call back Friday at 25:00",
        "Call back Friday or Monday",
        "Do not call back Friday",
        "Call back Friday and call back Monday",
        "Call back 2026-11-01 at 09:00",
    ],
)
def test_ambiguous_or_past_promises_require_an_exact_date(message):
    with pytest.raises(ValueError):
        resolve_callback(update(message, callback_timezone="UTC"), AT)


def test_no_timezone_does_not_guess_and_explicit_override_resolves_ambiguity():
    with pytest.raises(ValueError, match="hospital timezone"):
        resolve_callback(update(), AT)
    due, _ = resolve_callback(
        update("Call back when ready", callback_date="2026-11-20", callback_timezone="UTC"), AT
    )
    assert due.day == 20
    assert resolve_callback(update("Still processing"), AT) is None
    assert resolve_callback(update(clear_callback=True), AT) is None
    assert (
        resolve_callback(ProviderUpdate(status="RECEIVED", message="Thanks for the callback"), AT)
        is None
    )


@pytest.mark.parametrize("day,clock", [("2027-03-14", "02:30"), ("2027-11-07", "01:30")])
def test_dst_gaps_and_ambiguous_local_times_require_clarification(day, clock):
    with pytest.raises(ValueError, match="DST"):
        resolve_callback(
            update(
                callback_date=day, callback_local_time=clock, callback_timezone="America/New_York"
            ),
            AT,
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"callback_timezone": "not/a-zone"},
        {"callback_at": "2026-11-20T09:00:00"},
        {"callback_date": "2026-11-20"},
        {"callback_local_time": "25:00"},
        {"callback_at": "2026-11-20T09:00:00Z", "clear_callback": True},
        {"callback_at": "2026-11-20T09:00:00Z", "callback_date": "2026-11-20"},
    ],
)
def test_invalid_schedule_contracts_are_rejected(fields):
    with pytest.raises(ValidationError):
        update(**fields)

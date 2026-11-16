"""Conservative callback-date resolution; provider prose never changes contact policy."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DIRECTIVE = re.compile(r"\b(?:call\s+back|callback|follow[ -]?up)\b", re.IGNORECASE)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SCHEDULE = re.compile(
    r"(?:on\s+)?(?P<day>today|tomorrow|(?:next\s+)?(?:"
    + "|".join(WEEKDAYS)
    + r")|\d{4}-\d{2}-\d{2})(?:\s+at\s+(?P<clock>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?[.!]?",
    re.IGNORECASE,
)
CLARIFY = "Choose an exact callback date/time and hospital timezone for this provider message."


def local_instant(day: date, clock: str, zone_name: str) -> datetime:
    wall = datetime.combine(day, time.fromisoformat(clock))
    zone = ZoneInfo(zone_name)
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    instant = first.astimezone(timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) != wall:
        raise ValueError("Callback time does not exist in the hospital timezone (DST). " + CLARIFY)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("Callback time is ambiguous (DST); supply callback_at with an offset.")
    return instant


def clock_value(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.lower())
    if not match:
        raise ValueError(CLARIFY)
    hour, minute, period = int(match[1]), int(match[2] or 0), match[3]
    if minute > 59 or (period and not 1 <= hour <= 12):
        raise ValueError(CLARIFY)
    if period:
        hour = hour % 12 + (12 if period == "pm" else 0)
    elif match[2] is None or hour > 23:
        raise ValueError(CLARIFY)
    return f"{hour:02}:{minute:02}"


def resolve_callback(update, at: datetime) -> tuple[datetime, str] | None:
    """Resolve only explicit appointments or a single unambiguous callback directive.

    A bare weekday uses the next future occurrence at the supplied default local
    time (09:00 by default). 'Next Friday' always excludes today. A dated promise
    in the past is rejected, not silently shifted to another week.
    """
    if update.status == "RECEIVED" or update.clear_callback:
        return None
    zone_name = update.callback_timezone
    if update.callback_at:
        instant = update.callback_at.astimezone(timezone.utc)
        zone_name = zone_name or "UTC"
    elif update.callback_date:
        instant = local_instant(update.callback_date, update.callback_local_time, zone_name)
    else:
        directives = list(DIRECTIVE.finditer(update.message))
        if not directives:
            return None
        if not zone_name or len(directives) != 1:
            raise ValueError(CLARIFY)
        directive = directives[0]
        prefix = update.message[: directive.start()].lower()
        if re.search(r"\b(?:no|not|never|don't|cannot|can't)\b", prefix):
            raise ValueError(CLARIFY)
        match = SCHEDULE.fullmatch(update.message[directive.end() :].strip())
        if not match:
            raise ValueError(CLARIFY)
        local_now = at.astimezone(ZoneInfo(zone_name))
        day_text = match["day"].lower()
        clock = clock_value(match["clock"]) if match["clock"] else update.callback_local_time
        weekday = day_text.removeprefix("next ")
        if weekday in WEEKDAYS:
            days = (WEEKDAYS.index(weekday) - local_now.weekday()) % 7
            if days == 0 and day_text.startswith("next "):
                days = 7
            day = local_now.date() + timedelta(days=days)
            instant = local_instant(day, clock, zone_name)
            if instant <= at:
                instant = local_instant(day + timedelta(days=7), clock, zone_name)
        else:
            day = (
                local_now.date() + timedelta(days=day_text == "tomorrow")
                if day_text in ("today", "tomorrow")
                else date.fromisoformat(day_text)
            )
            instant = local_instant(day, clock, zone_name)
    if instant <= at:
        raise ValueError("Callback must be in the future. " + CLARIFY)
    return instant, zone_name


def routine_relative_callback(message, received_at):
    """Only a plain timing update; additional conditions never bypass interpretation."""
    match = re.fullmatch(
        r"\s*(?:(?:I |we )?(?:don['’]t have(?: (?:it|them|the records))? (?:currently|yet)|"
        r"(?:the )?records (?:are |aren['’]t )?(?:not ready|unavailable|ready yet))[,.; ]+)?"
        r"(?:please )?(?:call(?: us| me)? back|follow[ -]up) in "
        r"(?P<days>[1-9]|[12][0-9]|30|one|two|three|four|five|six|seven) days?[.! ]*",
        message,
        re.I,
    )
    if not match:
        match = re.fullmatch(
            r"\s*(?:(?:I|we) will |(?:I|we)['’]ll |will )send (?:you )?(?:the )?"
            r"(?:docs|documents|records) in "
            r"(?P<days>[1-9]|[12][0-9]|30|one|two|three|four|five|six|seven) days?"
            r"[,.; ]+(?:please )?call(?: us| me)?(?: back)? (?:then|at that time|that time)[.!\s]*",
            message,
            re.I,
        )
    if not match:
        return None
    value = match["days"].lower()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
    days = words[value] if value in words else int(value)
    return received_at + timedelta(days=days)

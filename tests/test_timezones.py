"""Browser timezone aliases must work even when the container omits OS aliases."""

import os
import subprocess
import sys


def test_timezone_aliases_work_without_system_timezone_files():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from datetime import datetime, timezone
from pydantic import ValidationError
from agents_platform.assignment_router import DescribeAssignment
from agents_platform.monthly_check_ins import CreateMonthly, calendar_due

request = DescribeAssignment(description='collect bills and records from that hospital',
                             timezone='Asia/Calcutta')
assert request.timezone == 'Asia/Calcutta'
schedule = CreateMonthly(client_contact_id='00000000-0000-0000-0000-000000000001',
                         timezone=request.timezone, day_of_month=5, local_time='09:00')
assert calendar_due(2027, 1, 5, '09:00', schedule.timezone) == datetime(
    2027, 1, 5, 3, 30, tzinfo=timezone.utc)
try:
    DescribeAssignment(description='collect records', timezone='Not/AZone')
except ValidationError:
    pass
else:
    raise AssertionError('Unknown zones must still be rejected')
""",
        ],
        env={**os.environ, "PYTHONTZPATH": ""},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr

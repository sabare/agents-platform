# Monthly client check-ins

A saved calendar date wakes the case supervisor. Case coordination checks whether
contact is appropriate and creates one client-check-in task when it is. Routine
calendar decisions use deterministic application rules, not an extra model call.
The specialist uses its normal agent workflow to send, wait for a reply and report back.
All outreach remains simulated.

## Set it up

In **Add task**, keep **Supervisor**, then choose **Monthly client check-ins** as the
work type. Choose the client, objective, message, calendar day, time and IANA timezone.
The schedule is added without replacing the case's records objective.

Defaults are 12 monthly slots and a **28-local-calendar-day minimum gap** after the
latest sent check-in or attributed client reply. The gap is configurable from 1–365
days. Optional start/end dates bound the calendar; the hard limit is 120 monthly slots.
Skipped and missed months count toward that limit. A missing day, such as February 31,
uses that month's final day without moving later months off the 31st.

Open the schedule group to see upcoming timing, month-by-month decisions and task
links. It also appears under the case in **Supervisor**. Pause/resume/stop require
an explicit reason. Sent replies and reviews can still finish after a stop.

## What if someone already checked in?

Example: the calendar is due January 5, but a teammate completed a manual check-in
January 3. The supervisor records January as **Skipped**, links the manual task,
and keeps February 5 as the next calendar date. It does not send again two days later
or move the calendar to 28 days after the manual task.

Both sent messages and client replies count. A recent reply to an older message can
therefore cover the current month. The scope is the same firm, case and client;
another client's or another firm's interaction does not suppress this schedule.
A comment or an unrecorded phone call is not automatically treated as a check-in.

## Cases the system handles

| Situation | Result |
|---|---|
| Recent manual or automatic check-in/reply | Skip the month and link the covering task |
| Due/nearby pending check-in | Reuse its coverage; do not create another message |
| Unsent manual task farther in the future than the contact gap | Does not suppress an earlier monthly slot |
| Prior message still awaiting reply/completion | Suppress another monthly send, even after 28 days |
| Open sensitive-report review | Block for human review; do not contact again |
| Applicable case contact hold | Block; an explicit release can resume the same occurrence within its window |
| Hospital-only hold | Does not become a client-wide restriction |
| Manual contact recorded after a monthly task was created | Recheck before send; suppress/cancel the unsent monthly task when coordination next reviews it |
| Concurrent timer ticks or retried delegation | One saved month and at most one monthly child task |
| Paused schedule | No automatic send; an unsent paused monthly task does not prevent a manual assignment |
| Stop or case closure | Prevent sending; cancel unsent monthly work when processed |
| Missed date or expired dispatch window | Record a missed/expired month; no catch-up message |
| Failed monthly task | Pause the calendar for inspection rather than silently creating a replacement |

The dispatch window is 24 hours after the due time, shortened by the schedule end
bound. After downtime, older slots are recorded as missed; only a still-valid due
slot can be delegated. Resume does not backfill dates that passed while paused.
Existing unsent work can resume only if its window, contact spacing and rules still allow it.

The spacing check runs at assignment and immediately before sending, with a shared
contact lock to serialize competing sends. Manual check-ins also respect the spacing
policy once configured. Stopping/finishing the calendar does not erase that policy;
a later approved schedule can configure a different gap. There is no automatic
urgent-contact override. Existing fixed-delay series and live monthly schedules
cannot coexist for the same client/case.

## Timezones and stored state

The calendar uses the chosen IANA timezone. An ambiguous daylight-saving wall time
uses its first occurrence; a nonexistent time shifts forward by the transition gap.
The minimum gap counts local calendar dates, so a daylight-saving hour change does
not falsely shorten an ordinary month.

`monthly_check_in_schedules` stores the approved cadence, spacing and next due time.
`monthly_check_in_occurrences` stores each stable schedule/month identity, decision,
reason, covering task and delegated child. A unique constraint protects each month.
The scheduler saves an inbox/outbox wake for case coordination; it does not call the
specialist itself. The supervisor saves a source-backed decision and starts the
normal `client_check_in` task under case coordination. Published records-only model
definitions remain unchanged; arbitrary free-text check-in planning is not enabled.

## API and checks

Prefix routes with `/v1/firms/{firm}/cases/{case}/monthly-check-ins`:

- `POST /`: create with `client_contact_id`, `timezone`, `day_of_month`, `local_time`
  and optional start/end, gap, count, objective and message fields. Requires `Idempotency-Key`.
- `GET /` or `GET /{schedule}`: inspect configuration and monthly outcomes.
- `POST /{schedule}/control`: `PAUSE`, `RESUME` or `STOP`, with `expected_version`,
  `reason` and `Idempotency-Key`. Exact retries reuse the prior change.

The implementation is in `monthly_check_ins.py`; the shared contact decision is in
`check_in_contact.py`. Tests cover seven calendar months, direct-task suppression,
late contact changes, reviews/holds, date boundaries, missed windows, parallel ticks
and a real Temporal timer/worker restart. See [development](development.md) to run them.

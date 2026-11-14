# Operations and correlated logs

Open **Operations** in the dashboard. The view is firm-wide, independent of case,
search and task-list pagination. Its authenticated API is
`GET /v1/operator/firms/{firm_id}/operations`. Counts include every matching row;
drill-down samples are limited to the oldest 10 items per signal. Snapshots use a
repeatable-read transaction and include an `as_of` time.

| Signal | Definition | Default alert |
|---|---|---|
| Pending outbox | All PENDING messages, including messages in retry backoff | Oldest message >= 60 seconds |
| Runnable work delay | Oldest unconsumed inbox event on an active specialist, or initial PENDING task | >= 120 seconds |
| Failed tasks | Current FAILED tasks, including supervisor group tasks | Any |
| Overdue callbacks | Nonterminal records requests whose callback is past due; includes blocked work | 60 seconds beyond deadline |
| Human requests | OPEN shared human requests, with oldest age and past-due count | Any past-due request, or oldest >= 24 hours |

Runnable work excludes future provider callbacks, scheduled client check-ins,
pending human-authorized continuations before their ready time, and BLOCKED or
terminal tasks. This is an end-to-end backlog-age indicator, **not an exact
Temporal schedule-to-start measurement**: running activities with unread inbox
entries also contribute. Actual Temporal schedule-to-start milliseconds are logged
when each instrumented activity starts. Intentional timer-only waits without an
unread inbox event do not count as queue delay.

The API process checks firms every 15 seconds even with no browser open. It logs
`operations.alert_opened` and `operations.alert_cleared` on condition transitions;
changes to an already-active count do not repeat the warning. Monitor failure and
recovery are also logged once per transition. This runs separately from the worker,
so a stopped worker can still produce backlog alerts while the API remains alive.
The dashboard refreshes on its existing 5-second cycle.

Thresholds are configured through `OPS_POLL_SECONDS`, `OPS_OUTBOX_AGE_SECONDS`,
`OPS_WORKER_DELAY_SECONDS`, `OPS_CALLBACK_GRACE_SECONDS`, and
`OPS_REVIEW_AGE_SECONDS` in `.env` (also passed through Compose).

Alerts currently go to the dashboard and process logs; no email, Slack or paging
service is configured. Transition suppression is in memory per API process:
restarting re-emits still-open alerts, and multiple API replicas each emit their
own transitions. If the API itself is down, an external uptime/log monitor is
needed. Historical alert retention is determined by the log collector, not a new
application table. Use one API process for this demo.

## Correlation and safe fields

Application telemetry emits one JSON object per line with:

- UTC timestamp, level, logger and stable event name.
- `request_id`, `firm_id`, `case_id`, `task_id`, `execution_id`, `event_id`,
  `workflow_id`; values are null when the lifecycle event has no such identity.
- Relevant status, attempt, cycle, duration, error type, tool name or action key.

HTTP responses include a generated `X-Request-ID`. HTTP logs use route templates,
not raw URLs, query strings or bodies. Activity logs cover records/check-in
execution, supervisors, series and monthly scheduling; Temporal calls additionally
include attempt and schedule-to-start time. Execution, tool, business-event and
outbox row changes log **after database commit**, with IDs joined to their task.
Rollback drops their pending log records. Core-SQL records task creation also has
an explicit post-commit lifecycle log. Tool action keys identify logical effects;
a cached/replayed effect is not a fresh contact.

No provider/client messages, documents, model prompts/responses, credentials,
headers or raw exception messages are serialized by the telemetry emitter. Errors
record their class. Existing persisted audit evidence remains the source for
business content. This is correlated lifecycle logging, not distributed OpenTelemetry
tracing or a complete trace of every internal model call. Existing third-party
loggers retain their own message behavior; production log collection still needs
appropriate access and retention controls.

```sh
docker compose logs --no-log-prefix api worker
```

Filter JSON log records by `task_id` to connect API business changes, execution,
tools, outbox messages and Temporal workflow IDs. `request_id` correlates a single
HTTP request; durable IDs connect later worker turns after that request ends.

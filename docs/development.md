# Run and extend the platform

## First setup

```sh
cp -n .env.example .env
docker compose up --build -d
docker compose run --rm migrate python -m agents_platform.seed
```

Seeding creates fictional contacts/cases and publishes agent definitions. It is
repeatable and never overwrites an already published definition.

| Surface | Address |
|---|---|
| Dashboard | http://localhost:8000 |
| Generated API reference | http://localhost:8000/docs |
| Liveness / readiness | http://localhost:8000/health/live · http://localhost:8000/health/ready |
| Temporal UI | http://localhost:8233 |
| Local PostgreSQL | localhost:5432; database/user/password `agents` |

Enter the operator credential through the dashboard connection settings or Swagger
**Authorize**. It defaults to `local-development-only`. This credential grants local
all-firm access; it is not production per-user authorization. OpenAI keys stay server-side.

The dashboard is plain HTML/CSS/JavaScript served by FastAPI; it needs no frontend
build server. PostgreSQL and Temporal use persistent volumes. `docker compose down`
preserves them; adding `-v` deletes them. Temporal's local development server is not
a production deployment.

## Updating an existing stack

Build first. Stop API and worker before a schema migration so old and new memory
storage layouts cannot be used at the same time. Take a database backup before
migration; use a fresh backup filename each time.

```sh
docker compose build api worker migrate
docker compose stop api worker
mkdir -p work/backups
umask 077
docker compose exec -T postgres pg_dump -U agents agents > work/backups/pre-upgrade.sql
docker compose run --rm --no-deps migrate
docker compose up -d --no-deps --wait api worker
docker compose run --rm --no-deps migrate python -m agents_platform.seed
```

Check readiness and logs. If migration fails, keep the application processes stopped
until the failure is resolved. Memory downgrade deliberately refuses to discard
lifecycle settings that older code could not enforce. Definition seeding publishes
new versions; it does not silently upgrade existing tasks or coordinators.

## Host development and modes

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -c constraints-runtime.txt -e '.[dev]'
docker compose up -d postgres temporal
make migrate seed
make api
# Another activated terminal:
make worker
```

Choose host or Compose API/worker processes; they otherwise share the configured
database and task queue. `constraints-runtime.txt` pins the tested runtime dependencies.
Docker caches their installation separately from source changes.

`.env.example` lists settings. `AGENT_MODE=scripted` uses deterministic SDK responses;
`AGENT_MODE=openai` requires `OPENAI_API_KEY`. Durable definitions select their model.
`OPENAI_MODEL` configures the legacy CLI scaffold. Outreach remains simulated.

Demo defaults are a 30-second follow-up cap and six automatic specialist cycles
before review. Context defaults are 12,000 estimated input tokens, 1,500 output
tokens and four retrieval/read calls per activation. These are demo/per-call limits,
not a complete run-cost policy. See [memory and retrieval](memory-and-retrieval.md).

## Product walkthrough and demos

Start with [task assignment](task-assignment.md). Supervisor is the default in the
shared task form. The supervisor delegates records through its model path and approved
monthly check-ins through deterministic coordination. Choose **Monthly client check-ins**
for the calendar settings. The existing interval series still uses a fixed delay after completion. **Memory & context** exposes the new memory lifecycle controls.

Tracked demo commands, run in the activated host environment against the local stack:

| Command | Demonstrates |
|---|---|
| `python examples/demo.py --restart-worker` | Records collection, waiting, restart and completion |
| `python examples/supervisor_demo.py --restart-worker` | Records delegation, reuse and objective verification |
| `python examples/memory_demo.py` | Evidence retrieval, holds, obligations and restart |
| `python examples/check_in_demo.py` | Scheduled messages, changed replies and review |
| `python examples/human_intervention_demo.py` | Exhausted records reminders, one authorized extra attempt and same-task completion |
| `python examples/client_review_demo.py` | Guidance, explicit acknowledgement and same-task resumption |

Demos add fictional data. Restart scenarios briefly stop the shared local worker.
The original `agents-platform "assignment"` CLI calls specialists inline; the durable
product path uses the API, saved tasks, dispatcher and worker.

## Main API paths

Prefix these with `/v1/firms/{firm}`. Use [Swagger](http://localhost:8000/docs) for
exact request/response schemas. Creation/intake uses `Idempotency-Key`: exact retries
reuse the saved result; conflicting reuse returns `409`.

| Route | Purpose |
|---|---|
| `POST /tasks/medical-records` | Assign records work; sample in `examples/medical-records-task.json` |
| `POST /tasks/{task}/provider-updates` | Simulate provider progress or synthetic document receipt |
| `POST /tasks/client-check-in` | Schedule one client check-in |
| `POST /tasks/{task}/client-replies` | Save one attributed reply after sending |
| `POST /tasks/{task}/client-reviews/{review}/decisions` | Allow completion or request changes, with reason and current task/review versions |
| `GET/POST /cases/{case}/check-in-series` | List/create a bounded fixed-delay series |
| `POST /cases/{case}/check-in-series/{series}/control` | PAUSE, RESUME or STOP, with reason and current version |
| `GET/POST /cases/{case}/monthly-check-ins` | Inspect/create a supervised monthly calendar |
| `GET /cases/{case}/monthly-check-ins/{schedule}` | Read monthly decisions and covering/delegated task links |
| `POST /cases/{case}/monthly-check-ins/{schedule}/control` | Pause/resume/stop with reason, version and idempotency key |
| `GET /cases/{case}/client-reports` | Read the latest 100 reports, optionally filtered by client |
| `GET/POST /cases/{case}/supervisor` | Read or explicitly update the records objective |
| `GET /tasks/{task}`, `/events`, `/executions` | Inspect state, timeline and execution evidence |
| `POST /cases/{case}/context/retrieve`, `/context/read` | Use the scoped search/read service directly |

[Memory lifecycle](memory-lifecycle.md) lists memory/review/instruction routes.
Operator workspace, overview, list and activity routes live under `/v1/operator`.

Series require a timezone-aware first due time, an interval of at least 60 seconds,
and a count limit or end date. The hard cap is 100 occurrences. One live series per
client/case is allowed; existing open check-ins/reviews prevent overlap. Stop is final.

Records v3 reminder occurrences are 1–3. Backend checks enforce order, due time,
current request state, contact holds and stable `records-reminder/N` action keys.
The default demo policy uses three reminders at 30-second intervals.

Client-report decisions are `ALLOW_COMPLETION` or `REQUEST_CHANGES`; they resume
the same task and do not clear restrictions. Changed report evidence invalidates
acknowledgement. The compatible client-review endpoint remains available. Shared records and supervisor
intervention use the [human-work API](human-intervention.md); arbitrary takeover remains unfinished.

## Checks

```sh
make check   # Lint, formatting and CLI wiring
make test    # Tests without infrastructure
make smoke   # PostgreSQL and Temporal integration tests
```

Integration tests use disposable databases and isolated queues, not the application
database. Test credentials need create/drop-database permission;
`TEST_POSTGRES_ADMIN_URL` overrides the default connection. Use
`pytest --collect-only -q` for the current test inventory.

Coverage includes scope isolation, replay/duplicates, rollback, migration backfill,
waiting/restarts, evidence checks, report reviews, reminders and series controls.
Memory tests cover archived capacity, note expiry, effective-date fencing, separate
activation/review timers, instruction retirement and stale historical evidence.
Retrieval tests include seven months of distracting history, filters and overflow.
Scripted models and synthetic vectors test execution behavior; they do not establish
real-model answer quality or months of continuous production operation.

## Code map and adding a specialist

All modules below are under `src/agents_platform/`.

| Area | Main files |
|---|---|
| API/UI | `api.py`, `dashboard.py`, domain API modules, `static/` |
| Storage | `models.py`, `tasks.py`; root `migrations/` |
| Durable coordination | `coordination.py`, `dispatch.py`, `orchestration/` |
| Specialist execution | `execution.py`, `domains.py`, `medical_records.py`, `client_check_in.py` |
| Current memory and lifecycle | `memory.py`, `memory_lifecycle.py`, `memory_api.py` |
| Human work | `human_requests.py`, `human_request_handlers.py`; [guide](human-intervention.md) |
| Calendar coordination and contact spacing | `monthly_check_ins.py`, `check_in_contact.py` |
| Indexing and context tools | `memory_history.py`, `context.py` |
| Published definitions | `definitions/`, `registry.py`, `seed.py` |

`supervisor.py` belongs to the legacy inline CLI path; `coordination.py` implements
the durable case supervisor used by the dashboard.

To add a specialist: define typed inputs/results and saved state; implement its
adapter's load/tools/execute/validate/review behavior; register the binding; publish
an immutable definition; add intake/update surfaces and end-to-end tests. Supervisor
delegation needs separate explicit support. Registering an adapter does not add it
to the supervisor's planning choices.

The shared runtime owns identity, versions, memory, action keys, saved effects and
workflow outcomes. Adapters are trusted application code and use its supplied
transaction. They cannot overwrite core task fields or operational memory.

## Provider callback appointments

Records agent v3 tasks accept provider-specific callback appointments through
`POST /v1/firms/{firm_id}/tasks/{task_id}/provider-updates` and the dashboard's
**Simulate provider response → Callback schedule** fields. Contact remains simulated.

```json
{
  "status": "PENDING",
  "message": "Please call back Friday at 2pm.",
  "callback_timezone": "America/New_York"
}
```

The resolver supports a single callback directive with a weekday, `today`,
`tomorrow`, or an ISO date, optionally followed by `at 14:30` or `at 2pm`.
Without a time in the message, it uses `callback_local_time` (default `09:00`),
shown in the form. Relative dates use the supplied hospital IANA timezone, not
server time. Ambiguous prose and DST wall times require an exact appointment.
An operator can override prose with `callback_date` + `callback_local_time` +
`callback_timezone`, or an offset-aware `callback_at`. Past appointments are rejected.

The original response, resolved appointment, event, task inbox and outbox wake
are committed together. The records adapter returns an absolute `ready_at` to
Temporal; the normal short demo follow-up interval cannot shorten this wait.
Rescheduling invalidates in-flight work and replaces the timer when the outbox is
delivered. An ordinary pending response preserves an existing appointment;
`clear_callback: true` cancels it and restores the normal reminder interval.
Receiving all records clears the callback and wakes completion immediately.

At the agreed time, the existing bounded simulated reminder tool performs the
follow-up once and clears the appointment. It consumes one of the task's maximum
three reminders; an exhausted limit requires teammate review. Contact holds still
apply. Callback scheduling does not upgrade pinned agent versions or add voice calls.


Monthly calendar rules, 28-day default spacing, DST handling, 24-hour dispatch windows
and persistent manual-contact spacing are documented in [monthly check-ins](monthly-check-ins.md).
Run `pytest tests/test_monthly_calendar.py` for date/configuration checks and
`RUN_INTEGRATION_TESTS=1 pytest tests/test_monthly_check_ins.py` for orchestration,
contact suppression, seven-month progression and Temporal worker-restart coverage.


## Partial records receipt

Provider updates accept `status: "PARTIAL"` with a nonempty
`received_requirement_ids` list. IDs refer to the exact requirements returned by
`GET /tasks/{task}` (including their date scope), not just document types. The
dashboard provides **Receive selected items** checkboxes.

Only selected items receive synthetic evidence. The server derives `PARTIAL` versus
`RECEIVED` from accumulated evidence; it never removes received evidence on a later
processing update. A final selected delivery can use `PARTIAL` and will automatically
become `RECEIVED` when it covers the remaining required items. `RECEIVED` with IDs
requires full accumulated coverage. For compatibility, `RECEIVED` without IDs is an
explicit all-items delivery.

Exact retries reuse the source. Re-delivering an already received item under a new
key saves the provider message but does not duplicate its evidence. Invalid IDs
(including another task's requirements), empty/duplicate lists and contradictory
statuses are rejected. A task lock serializes concurrent deliveries; task versions
invalidate in-flight plans and sends.

Every reminder, including an additional human-authorized follow-up, snapshots the
currently missing requirement IDs, full date scopes and simulated message at send
time. Received bills are excluded from a later request for records and imaging.
Pending callbacks stay in effect after partial deliveries; complete receipt clears
them. Human authorizations still require a fresh review if provider information
changes after approval. No database migration is needed: existing requirement and
evidence rows provide the coverage state.

Run `RUN_INTEGRATION_TESTS=1 pytest tests/test_partial_receipts.py` for coverage and
`python examples/partial_receipt_demo.py` for a real API/worker walkthrough. Pass
`--leave-partial` to keep bills received and records/imaging outstanding for review
in the dashboard. Outreach and received artifacts remain simulated in both modes.


## Execution incidents and manual contact

See [recovery and contact](recovery-and-contact.md) for operator steps, API contracts,
recovery generations, contact spacing and current limits. The schema revision
`6e8b9252f1d6` follows shared human requests. Back up and migrate using the coordinated
procedure above before restarting API and worker.

`python examples/recovery_contact_demo.py` creates a controlled fictional post-send
failure, records a provider call, resumes the original assignment and checks that
its initial simulated send was not duplicated. Pass `--leave-failed` to inspect the
incident in the dashboard. Tests run in disposable databases and isolated queues.

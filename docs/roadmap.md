# What to build next

Checked against the code on 14 November 2026. This is the proposed work order, not
a delivery schedule. Items below are **not implemented** unless explicitly noted.

The task form and memory lifecycle are now in place. The next product goal is:

> A saved monthly schedule wakes the supervisor. It reads current rules and work,
> then delegates a client check-in when appropriate.

## 1. Let the supervisor delegate check-ins

The check-in specialist already exists. The missing connection is the supervisor's
records-only objective/action schema and assignment logic.

- Add an approved check-in assignment type alongside records targets: client, time,
  objective and message. Support mixed records/check-in objectives.
- Reuse the existing check-in creation, reply, review and action-time hold checks.
  Save the child task and wake intent together, under the supervisor's parent task.
- Reuse existing covering work. Do not create a duplicate when a reply or review is
  pending. Show whether the decision delegated, reused work or requested help.
- Define completion per assignment type. Finishing records must not incorrectly
  finish a mixed objective with outstanding check-in work.
- Publish a new supervisor definition; keep existing pinned versions unchanged
  unless an explicit upgrade/migration is chosen.

**Done when:** a supervisor objective creates the right specialist tasks, and retries,
concurrent decisions, changed scope and new restrictions cannot create extra sends.

## 2. Add calendar-month schedules that wake the supervisor

Keep this distinct from today's series, which waits an interval after completion
and creates check-in tasks directly.

Save the client, case, calendar day, local time, timezone, active/paused/stopped
state, next due time and end bound in PostgreSQL. Use Temporal for waiting.
The proposed default for a day missing from a month is its last day.

Give each scheduled month a stable identity, such as schedule ID + `2027-04`.
A due occurrence should wake case coordination once; the supervisor decides whether
to delegate, reuse pending work, skip that occurrence or request review. Record the
reason. Missed dates should not produce a burst of catch-up messages by default.

Define how these schedules coexist with manual check-ins and existing live series;
never run both outreach paths for the same client without an explicit policy.
Pause/stop and changed schedule scope must invalidate inappropriate pending work.
Neither a new month nor a supervisor decision can lift a contact hold.

**Done when:** the configured monthly date reaches the supervisor reliably after
restarts, with no duplicate monthly occurrence or overlapping client outreach.

## 3. Verify the complete monthly flow

Use a controllable clock and scripted agents to test months of state changes without
waiting months. Cover:

- February, leap years, the 29th–31st, timezones and daylight-saving changes.
- Duplicate ticks, worker restarts, missed dates and a crash after saving an assignment.
- An unanswered prior check-in, an open report review and a paused/stopped schedule.
- A new, reviewed, replaced or resolved hold while the supervisor is thinking.
- Ordinary completion, skipped occurrences, blockers and clear operator history.

Then run a small labelled real-model evaluation for delegation/context decisions.
Execution tests alone cannot establish the quality of model judgement.

## Later work

| Area | What remains |
|---|---|
| Human intervention | General records/objective review resolution, takeover, report corrections and individual reviewer identity |
| Memory quality | Better document chunks, optional reranking, suggested duplicate/conflict changes and tests of updated/delayed facts |
| Scale | Batched policy-change fan-out, backlog partitioning, large-case context handling and retrieval measurements |
| Real integrations | Messaging/provider systems, real document intake and reconciliation of uncertain delivery outcomes |
| Production operations | Per-user firm/case authorization, stronger tenant protection, retention/deletion, monitoring and load/recovery testing |

Memory already supports explicit review, supersession, resolution, note expiry and
instruction retirement. **Automatic free-text contradiction resolution is still
missing.** Retrieval dates still refer to source recording time, not arbitrary
fact-validity time. Active task memory is capped at 100 open, unexpired items;
archived history does not consume that limit. Large essential context asks for help.

## Design choices to keep

PostgreSQL owns business state; Temporal owns progression and waiting. The supervisor
proposes, and backend code validates. Current controls load directly through SQL;
history is retrieved as evidence. Preserve source links, explicit versions and saved
action identities. A graph database or another memory framework should be introduced
only if measurements show a useful advantage over the current PostgreSQL/pgvector setup.

Useful research behind the memory work:

- [LongMemEval](https://arxiv.org/html/2410.10813v2): evaluate changing facts, time reasoning and insufficient evidence.
- [Zep](https://arxiv.org/html/2501.13956v1): preserve history and distinguish when information applied from when it was recorded.
- [Mem0](https://arxiv.org/html/2504.19413v1): reconcile incoming memory rather than only appending it.

These are design references, not claims that their complete systems or benchmark
results are reproduced here. Keep current behavior in the implementation guides
and remaining work in this roadmap.

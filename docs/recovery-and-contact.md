# Failed-task recovery and manual contact

An execution incident is the operator queue for an agent assignment whose execution
retries are exhausted. It is similar to a dead-letter queue, but retains business
identity, saved effects and validated recovery actions. No additional broker is needed.

## Operator walkthrough

Open **Execution incidents**, inspect the failure category and saved actions, and
choose **RETRY** or **CANCEL** with a reason. Optionally record a manual conversation
in the same decision. Alternatively use **Record a call or other contact** on a task.

A retry preserves the task ID, pinned agent version, requirements, evidence and
logical action keys. It creates a new Temporal workflow identity and a new cycle
with fresh context. The incident links the previous and next workflow identities;
prior executions remain untouched. Late old-generation activities and dispatch
messages cannot overwrite the recovered assignment. A saved transactional simulated
send is reused rather than repeated. There are at most three operator recoveries
per assignment. Each repeat failure creates another incident. When the limit is
reached, cancellation is available; broader repair/migration needs engineering.

Automatic activity retries remain the first response to transient failures. An
incident is created only after those retries exhaust. Worker startup idempotently
backfills existing failed specialist tasks. Failure types are shown without raw
exception messages or stack traces. Failed case coordinators and dispatch failures
are not included in this first specialist recovery queue.

`OUTCOME_UNKNOWN` incidents cannot be blindly retried. All currently implemented
outreach is transactional simulation. Real integration adapters will need durable
provider delivery IDs, reconciliation handlers and an explicit resolution for
unknown delivery. Cancelling an assignment stops future actions; it does not assert
that an uncertain external action never happened.

## Manual contact semantics

Contact records capture the person contacted, actual contact time, channel, outcome,
operator notes and optional case-scoped evidence source IDs. Recording time and
actual contact time are separate. All records are attributed to the authenticated
`local-operator`; per-user identity remains unfinished. A record is an attributed
account, not independent verification of its contents.

- `REACHED` client contact contributes to the existing monthly contact-spacing policy.
- Voicemail, no answer and wrong-contact outcomes do not count as a reached conversation.
- A contact record does not create a client report, acknowledge a sensitive report,
  mark documents received, clear a hold or satisfy an existing pending check-in.
- The contact's person and evidence must belong to the task's case.
- Source-backed history is retained and available to subsequent activations.
- Recording contact on a failed task does not resume it. Resolve its incident explicitly.

Optionally select an exact provider follow-up and response deadline. Both must be
provided, the outcome must be `REACHED`, and the person must be this records task's
provider. This uses the existing explicitly authorized one-follow-up path; automatic
reminder limits and agent definitions remain pinned. Restrictions are checked before
accepting authorization and again before sending. Follow-up requests only missing
items. If the person wants to save facts while a hold is active, omit the follow-up
plan; do not treat the recorded conversation as new permission.

An incident decision can atomically save the contact and schedule, resolve the
incident, and queue recovery. Invalid plans roll back the whole submission.

## API

Prefix with `/v1/firms/{tenant_id}` and use the operator bearer credential.

| Route | Purpose |
|---|---|
| `GET /incidents` | Open queue; optional task filter, `status=ALL`, offset and limit |
| `POST /incidents/{id}/resolve` | Retry or cancel with reason and current task/incident versions; optional typed contact |
| `GET /tasks/{id}/manual-contacts` | Paginated attributed history ordered by actual contact time |
| `POST /tasks/{id}/manual-contacts` | Record a conversation with current task version; optional explicit follow-up plan |

Writes require `Idempotency-Key`. Exact retries return the saved operation; different
content with the same key and competing/stale recovery decisions return `409`.
Validation/scoping failures do not leave partial effects. Decisions, sources,
business updates, events, inbox and outbox requests commit together. A successful
response means the decision was saved and continuation queued, not that the resumed
work has completed. Inspect task state and history for the outcome.

A contact payload looks like:

```json
{
  "contact_id": "CASE_CONTACT_UUID",
  "occurred_at": "2026-11-14T10:00:00Z",
  "channel": "PHONE",
  "outcome": "REACHED",
  "notes": "Spoke to records department. Remaining imaging expected Friday.",
  "evidence_source_ids": []
}
```

`follow_up_at` and `response_due_at` can be added for explicit authorization. Choose
future times (up to 90 days for follow-up, with a response deadline within 30 days
of that follow-up). The system does not derive policy changes from free text.

## Boundaries and next steps

This is specialist execution recovery, not Temporal reset/replay tooling or a generic
outbox DLQ. If the database is unavailable through failure-finalization retries,
Temporal operational recovery may still be needed before an incident can be saved.
External notifications, individual reviewer authorization, evidence correction and
actual call/email integrations remain future work. Cancellation is irreversible
through the recovery UI; it preserves all received evidence and audit records.

Use the coordinated backup/migration procedure in development.md. The migration
refuses downgrade when it would discard manual contact or resolved incident history.

```sh
RUN_INTEGRATION_TESTS=1 pytest tests/test_recovery_contact.py
pytest tests/test_human_contracts.py
python examples/recovery_contact_demo.py
python examples/recovery_contact_demo.py --leave-failed
```

The demo creates fictional data, sends one simulated initial request, injects a
controlled post-send failure, then uses the real API/worker to record a call, recover,
send the authorized missing-item follow-up and complete. It asserts exactly one
saved initial send. The `--leave-failed` variant leaves an incident for UI review.

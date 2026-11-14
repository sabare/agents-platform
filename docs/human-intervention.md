# Human intervention

Human work is a durable request attached to an existing task. The platform stores
its question, evidence links, owner label, deadline, permitted decisions and audit
history. A domain handler validates what a decision means. A decision resolves its
request; the original task can still be waiting or blocked for another reason.

## Operator experience

Open **Needs your input** to see open requests, owners and overdue work. Open a task
to inspect evidence, choose a next step and record a reason. Each option explains
its consequence before submission. Owner and deadline changes are audited separately
from decisions. The queue supports pagination.

All decisions are attributed to the authenticated `local-operator`. Owner names
are responsibility labels, not user accounts or permissions. This prototype still
uses the shared all-firm development credential. Becoming overdue does not approve
an action or lift a restriction; external notifications/escalation are not implemented.

## Implemented paths

| Handler | Human actions | Result |
|---|---|---|
| Records follow-up | Schedule one additional follow-up, recheck, stop collection | Waits until an exact date; sends one simulated follow-up; waits for a bounded response deadline. Recheck reloads state. Stop cancels collection. |
| Client report | Acknowledge handled, request changes | Preserves the existing report fingerprint, explicit acknowledgement and same-task resumption. Guidance keeps review open. |
| Supervisor objective | Clarify the approved objective and records targets | Validates participant roles, replaces scope and wakes the same supervisor. Existing specialists are retained. |

An authorized records follow-up is a deterministic operator action, not an expansion
of the pinned agent's tools. Automatic reminder limits, action keys and published
definitions remain unchanged. Each decision authorizes one additional attempt only. Its saved message and
requirement IDs cover only items still missing at send time.
The action is simulated, just like existing outreach.

Follow-up must be in the next 90 days. Its response deadline must be after the
follow-up and within 30 days. The runtime sleeps directly until those dates without
spending model turns or consuming the automatic demo activation budget.

Before sending, it rechecks current contact restrictions, provider role, case state
and the evidence/policy fingerprint reviewed by the person. Changed provider
information or policy cancels the authorization and creates a fresh request. Complete
records arriving before the send cancel it. If a worker misses the whole authorized
window, the system requests review rather than sending a late catch-up message.
After a send, absence of complete records by the response deadline produces another
actionable request. No human decision manufactures records evidence.

## Storage and progression

- `HumanRequest`: shared lifecycle, evidence references, actions, owner and deadline.
- `HumanDecision`: append-only responses, actor, reason, input hash and source link.
- `HumanContinuation`: mutable delivery state and due dates for a permitted action.
- `ClientReview`: retained as the client domain's acknowledgement record; both the
  old endpoint and the shared endpoint update the same request/decision ledger.

Submission checks the task and request versions, permitted action, typed payload,
current evidence and domain rules. The decision, domain changes, event, inbox and
outbox are committed together. Exact retries reuse the decision; conflicting reuse
or a competing/stale response returns `409`. Invalid domain responses return `422`.
No model call or network delivery occurs inside the submission transaction.

The API acknowledges a **saved decision and queued continuation**, not successful
execution of the next action. Temporal wakes the existing task, which reloads current
state. A transactional continuation row and unique source identity prevent duplicate
simulated sends if an activity is retried after committing the effect.

Migration backfills existing client reviews and decision history. Worker startup
idempotently exposes pre-upgrade blocked records and supervisor questions as requests.
Use the coordinated backup/migration procedure in [development](development.md).
Downgrade refuses to discard new domain decisions.

## API

Prefix routes with `/v1/firms/{tenant_id}` and use the operator bearer credential.

| Route | Purpose |
|---|---|
| `GET /human-requests` | Open queue; optional `task_id`, `status`, `offset`, `limit`. `status=ALL` includes history. |
| `GET /human-requests/{id}` | Request, decision history, continuation state and current task version |
| `POST /human-requests/{id}/assignment` | Set owner label/deadline with expected request version and idempotency key |
| `POST /human-requests/{id}/decisions` | Submit a permitted typed decision with task/request versions and idempotency key |

Example response to a blocked records request:

```json
{
  "expected_task_version": 9,
  "expected_request_version": 1,
  "action": "SCHEDULE_FOLLOW_UP",
  "reason": "Reviewed provider history; one additional attempt is appropriate.",
  "payload": {
    "follow_up_at": "2027-01-05T14:00:00-05:00",
    "response_due_at": "2027-01-12T14:00:00-05:00"
  }
}
```

Use future dates and versions returned by the current request. Responses expose each
action's payload JSON schema and consequence text. These descriptions do not grant
permission: the backend handler always validates again.

## Extension points and limits

Add a trusted handler with typed response models, evidence validation and explicit
state changes. A specialist can provide `request_human_help` and `advance_human_work`
hooks; the shared executor does not need another domain branch. Supervisor
clarifications use the same lifecycle through case coordination.

Current request kinds are clarification, approval, review and manual action. The
shipped paths cover clarification/review/manual decisions; arbitrary proposed-tool
approval is not implemented. Neither are arbitrary human takeover, document uploads,
reply corrections, new contact-permission grants or per-user authorization.
`REQUEST_CHANGES` saves client guidance and keeps review open; it does not claim to
have corrected a report. Shared requests do not automatically replace every existing
failure/status path. New domains must explicitly provide their supported responses.

## Verification

```sh
RUN_INTEGRATION_TESTS=1 pytest tests/test_human_requests.py tests/test_human_migration.py
pytest tests/test_human_contracts.py
python examples/human_intervention_demo.py
```

Tests use disposable databases and isolated Temporal queues. They cover a real
worker restart across the authorized timer, duplicate/concurrent decisions, stale
evidence, holds, early receipt, expired windows, response deadlines, cancellation,
supervisor clarification, client compatibility, assignment and upgrade backfill.

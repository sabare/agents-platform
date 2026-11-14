# Memory lifecycle

The database keeps current controls and their full source-linked history. The
model receives only effective open controls, active instruction revisions and
bounded optional notes. A larger archive does not become a larger mandatory prompt.

## Storage and migration

- `task_memory_items` holds task obligations, contact holds and informational notes.
  The migration preserves legacy JSON items, IDs, revisions, original/resolution
  source links and unrelated task progress, then removes the old duplicate JSON list.
- `case_restrictions` continues to hold whole-case and provider-specific contact holds.
- `instructions` holds each current published instruction; `instruction_revisions`
  preserves prior publications, including retirement and review/effective metadata.
- Sources preserve each submitted request plus the backend's saved result. New
  duplicate evidence remains saved even when it reuses an existing current item.

Task items have OPEN, RESOLVED, SUPERSEDED or EXPIRED status. Case holds cannot
expire. Instructions are ACTIVE or RETIRED. Effective dates, source recording dates
and closure dates are separate. This is not a general temporal fact-reconciliation
engine: contradictions in arbitrary prose are not automatically turned into policy.

## Writes

All routes require the existing operator credential. Task/case writes require
`Idempotency-Key` and the current `expected_version`; instruction publications use
`expected_revision`. Repeated keys return the original saved result, including
after a note expires. Stale versions and cross-firm/case/task references fail.
The local operator identity is still a development identity, not production user RBAC.

`POST /v1/firms/{firm}/tasks/{task}/memory` supports:

| Operation | Required fields | Meaning |
|---|---|---|
| ADD | kind, text | Add or reuse an exact duplicate in the same task |
| RESOLVE | item_id, text | Explicitly close it; text records the reason |
| SUPERSEDE | item_id, kind, text, reason | Atomically replace it, retaining kind and the chain |
| REVIEW | item_id, text | Record review without lifting it; optional review_at schedules another review |

ADD/SUPERSEDE may supply `effective_from`, `review_at`, `due_at` and, for NOTE only,
`expires_at`. Due dates never expire obligations. New expiry dates must be in the
future. Replacements cannot be future-dated: that would retire a current control
before its replacement applied. Review dates can be due/overdue and never grant
permission. Resolution, supersession and retirement need an explicit operator action.

The equivalent case `/cases/{case}/restrictions` route supports the same operations
without kind, due or expiry fields. A replacement retains the original provider
scope; it cannot turn a case-wide hold into a provider-only hold. Both ADD paths
conservatively reuse equal text after whitespace/case normalization only when scope,
kind and lifecycle dates match. No semantic model can merge or lift hard restrictions.
Repeated corroboration with a changed review date should use REVIEW explicitly.

Instruction publication accepts `status: RETIRED` to remove the instruction from
current context while preserving its revision history and reason. Publishing ACTIVE
reactivates it explicitly. Future-dated instruction replacement is rejected so the
old current revision cannot disappear prematurely.

## Reading, timers and context

- Task `/memory` returns complete current controls and optional notes, plus at most
  100 historical task items. Use `history_offset`, `history_limit` and the returned
  `history_next_offset` for additional history. Archived rows do not consume the
  100-open-item task-memory limit. Genuinely large essential state still requests review.
- Case `/memory/reviews` lists applicable overdue task/case/instruction reviews with
  `offset`, `limit` and `next_offset`. Acknowledging review leaves the control active.
- The indexing activity processes up to 100 lifecycle transitions per sweep and
  persists event/wake intent in the same transaction. Replayed sweeps do not resend
  transitions. Effective dates activate controls; review dates wake work for review;
  NOTE expiry archives a note. Contact holds and obligations never time out.
- Mandatory SQL loading and action/commit fingerprints independently check current
  dates. A newly effective hold invalidates an old decision even before the timer
  sweep runs. Optional notes can be omitted to fit context; hard controls cannot.
- `retrieve_context` and `read_evidence` attach current lifecycle state to historical
  memory sources. Old evidence remains readable and cannot override current controls.

The task Context panel supports effective/review dates, note expiry, review,
replacement and resolution, with archived memory collapsed separately. Existing
workflows and specialists use the same shared memory loader. Policy changes still
fan out transactionally; very large firms will need batched fan-out. The 100-item
cap is an active-state guardrail, not a policy to discard important information.

Downgrade preserves legacy-compatible task rows back into JSON. It refuses to remove
lifecycle fields while retirement/timed settings/notes exist, because older code
could not enforce those settings safely.

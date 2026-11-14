# Keeping memory current

A case may last months. Its history should grow, while the agent's briefing contains
only the rules and responsibilities that currently apply.

Example: January's "do not contact Hospital A" stays active until an authorized
operator resolves or replaces it. A July comment saying "contact is fine now" does
not itself change the restriction. The original evidence remains readable afterward.

## States and dates

| Item | States | How it leaves current context |
|---|---|---|
| Task contact hold or obligation | OPEN, RESOLVED, SUPERSEDED | An operator resolves or explicitly replaces it |
| Case/provider contact hold | OPEN, RESOLVED, SUPERSEDED | An operator resolves or replaces it in the same scope |
| Informational task note | OPEN, RESOLVED, SUPERSEDED, EXPIRED | Resolution, replacement or its configured expiry |
| Published instruction | ACTIVE, RETIRED | Explicit retirement or a new revision of that instruction |

`effective_from` says when an item starts applying. `review_at` asks for another
review; it never lifts a hold. `due_at` is an obligation's due date, not an expiry.
Only NOTE items accept `expires_at`. Recording time and closure time are retained
separately. An old effective hold is not weakened merely because it is old.

## Use the dashboard

Open an agent task's **Memory & context** tab. Current/scheduled items and archived
history are displayed separately. Add a hold, obligation or optional note with
appropriate dates. Existing items offer:

- **Reviewed / keep active:** record a reason, optionally choose another review date.
- **Replace with a revised item:** provide replacement text and a reason. The old
  item becomes superseded and keeps its link to the replacement.
- **Resolve:** record why the item can be closed.

Task resolution does not clear a case hold. Client-report **Allow completion** is a
separate review decision and clears neither holds nor obligations. Instruction
retirement/publication and the complete overdue-review list are API features.

## What prevents growth and stale decisions?

Task items use dedicated `task_memory_items` rows, not an ever-growing progress JSON
list. The upgrade preserves old item IDs, sources, resolutions and unrelated task
progress. Case holds remain in `case_restrictions`; instructions keep a revision ledger.

ADD reuses equal text after whitespace/case normalization only when scope, kind and
lifecycle dates match. Every submitted change still has its own saved source/result.
Different wording is not automatically merged, and newer prose does not automatically
replace approved rules. Use REVIEW to change a review date; use SUPERSEDE to revise
an item deliberately.

Archived task history is paginated separately. It does not consume the limit of
100 open, unexpired task items. That active limit still includes scheduled items.
Oversized mandatory state requests human review rather than dropping controls.
Optional notes can leave the model briefing when space is limited.

The background worker processes up to 100 lifecycle transitions per sweep: newly
effective items, due reviews and note expiry. Distinct date transitions have distinct
saved event identities. Retries reuse them. Action-time date/version checks also
work independently, so a late sweep cannot let an agent act past a new restriction.
Retrieved historical memory sources show their current status and replacement link.

## API reference

Prefix these paths with `/v1/firms/{firm}`. Business routes require the development
operator credential. Task/case writes require `Idempotency-Key` and current
`expected_version`; instruction publication uses `expected_revision`.

| Route | Purpose |
|---|---|
| `GET/POST /tasks/{task}/memory` | Read current task memory/history; submit a change |
| `GET /cases/{case}/memory` | Inspect case holds, memory version and digest |
| `POST /cases/{case}/restrictions` | Change a whole-case or provider-specific hold |
| `GET /cases/{case}/memory/reviews` | List overdue applicable task/case/instruction reviews |
| `GET/POST /instructions/{instruction}/revisions` | Read history or publish/retire a revision |

Task operations are ADD (`kind`, `text`), RESOLVE (`item_id`, reason in `text`),
SUPERSEDE (`item_id`, same `kind`, new `text`, `reason`) and REVIEW (`item_id`, reason
in `text`, optional `review_at`). ADD/SUPERSEDE accept effective/review dates, a due
date and NOTE-only expiry. New expiry dates must be in the future. Case holds use
the same operations without kind/due/expiry fields and retain their provider scope.
Future-dated replacement is rejected so the old control cannot disappear early.

Instruction publication can explicitly set `status: RETIRED` or `status: ACTIVE`.
Editing a retired instruction without an explicit status does not reactivate it.
Omitted review/effective metadata is preserved; explicitly supplying null clears it.
Future-dated instruction replacement is rejected.

Task reads accept `history_offset`/`history_limit` and return `history_next_offset`.
The overdue-review queue uses `offset`/`limit` and returns `next_offset`.
Stale versions, cross-scope references and conflicting idempotency-key reuse fail.
Exact retries return the original result, including after a note has expired.

Automatic interpretation of contradictory free text is still unbuilt. Database
rollback to older code is refused while lifecycle settings it cannot enforce exist.
See [development](development.md) for upgrades and [roadmap](roadmap.md) for next work.

# What to build next

Checked against the code on 14 November 2026. This is the proposed work order, not
a delivery schedule. Items below are **not implemented** unless explicitly noted.

## Completed foundation

The shared task form, explicit memory lifecycle and supervised monthly calendars
are implemented. Calendar occurrences wake case coordination; deterministic rules
check recent sends/replies, manual tasks, pending reviews and holds before delegation.
The 28-day default spacing is checked again before sending. Unique month identities,
a 24-hour dispatch window, pause/stop controls and no catch-up bursts are covered by
tests, including seven months and a real Temporal worker restart.
See [monthly check-ins](monthly-check-ins.md).

## Next steps

1. **Record off-platform contact.** Add attributed, source-backed intake for phone
   calls or other channels, so recorded real contact can satisfy a monthly occurrence.
   Free-text comments must not silently become verified contact or permission.
2. **General check-in planning.** If needed beyond approved calendars, add typed
   check-in targets to the supervisor model's objective/actions and completion rules.
   Publish a new definition; do not silently change pinned records-only versions.
3. **Broader human intervention.** Resolve records/objective reviews, correct replies,
   support individual reviewer identity and provide deliberate takeover controls.
4. **Evaluate real-model decisions.** Use labelled cases for delegation, changing
   facts and context selection. Scripted execution tests do not establish judgement quality.

## Later work

| Area | What remains |
|---|---|
| Product refinements | Schedule editing, explicit contact-policy changes and richer operator review/navigation |
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

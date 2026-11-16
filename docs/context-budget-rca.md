# Context budget failure: root cause and fix

## What happened

The demo records task stopped after two reminders with “Essential state or tool history
cannot fit; operator review required.” It then showed a provider form offering another
follow-up. This was a platform error, not a missing authorization or a provider decision.

Saved execution: `88ddadae-d244-556b-8e65-07a9553f5251`, cycle 3.
The full saved context grew from about 4,970 to 9,072 tokens over four activations.
The last snapshot included 4,678 tokens of optional historical evidence. The failure
happened before a new model call, not because the provider rejected a request.

## Root causes

1. **Inconsistent packing and final counting.** `ModelBudget.filter()` gave
   `pack_context()` a budget for the context object. The final estimate also counted
   that object as escaped JSON inside a message, plus instructions, schemas and an
   overhead reserve. One packing pass left 8,702 context tokens, but its wrapped
   request was estimated at **12,352**, above the configured **12,000** limit.
   The filter stopped instead of removing more optional history. Nothing essential
   needed to be removed.
2. **Technical errors used a business-review path.** `_decide()` converted
   `ContextBudgetExceeded` into the domain adapter's `needs_human` decision. `_commit()`
   then created the usual records follow-up request. That form offered more outreach,
   which could not fix a context failure. The execution also appeared technically
   successful even though no business decision had been produced.

The 12,000-token figure is our configured input budget, not the model's maximum
context window. Increasing it would have hidden the packing defect.

## Changes

- Repack against the same wrapped-request estimate used by the final guard. Reduce
  optional history further until the request fits or no removable content remains.
  The packing target only decreases, so the loop terminates.
- Keep required task/domain state, instructions and operational memory. Keep tool
  call/result pairs and domain-action results. Older retrieval text may be shortened
  with its references retained. The original database snapshot is unchanged.
- Record true worker overflows as `FAILED` executions with a `CONTEXT_BUDGET` incident.
  The existing incident flow offers retry/cancel, preserving saved action keys.
  Do not create a provider approval or a fabricated model decision for this error.
- Save token-count diagnostics and a reason code without copying sensitive text.
  Do not emit a successful business-validation score when context preparation failed.
- Retire the legacy generic provider review when reclassifying the affected failure;
  authorization and communication controls remain in force.

## Verification

Read-only replay of the saved failing snapshot now estimates **11,530 / 12,000** tokens.
It removes **one historical passage**. The task, domain state, required instructions
and operational memory are unchanged. Replay makes no model calls or business writes.

```sh
python scripts/replay_context_budget.py 88ddadae-d244-556b-8e65-07a9553f5251
```

Regression coverage includes a request whose bare snapshot fits but message envelope
does not, optional-history compaction, unchanged original snapshots, protected memory,
tool call/result pairing, unreadable/oversized evidence, schema overflow and technical
incident recovery. Recovery tests verify that the initial send occurs only once.

A real case can still have more mandatory information than a configured budget allows.
That must stop visibly as an engineering incident, rather than discard controls or ask
for unrelated contact permission. Token estimates include a reserve; they are not an
exact reproduction of a provider's accounting for every model.

## Live result

The fix was deployed to the local API and worker. The existing demo task was recovered
through its incident API, retaining its task ID and saved actions. Real OpenAI calls
in recovery cycles 4 and 5 fitted at 11,724, 11,655, 11,723, 11,646 and 11,281 estimated
input tokens. No recovered call exceeded the unchanged 12,000-token limit. The initial
send remained a single saved action; the superseded workflow generation closed.

The task then reached its actual three-reminder limit. That normal business stop
remains enforced; the context fix does not authorize further outreach or count
missing documents as received. A provider response can continue the demonstration.
The focused regression run passed 72 tests; frontend checks passed 17 tests.

## Sources checked

OpenAI's [session-memory guide](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory)
describes trimming or compressing working history while keeping needed state.
Its [tool-output trimmer](https://openai.github.io/openai-agents-python/ref/extensions/tool_output_trimmer/)
runs before model calls and reduces older tool output. We retain the existing input-filter
hook and source references, with stricter protection for current controls and action results.
This fix does not replace durable memory with a model-generated summary.

# Interviewer walkthrough review

Reviewed on 15 November 2026. The focus was autonomous progress, observable decisions
and reuse of the platform across workflows.

## Hospital data

The original application database had 18 provider records across sample workspaces,
each called Example Hospital. The case previously used in the browser had one provider.
There are now 20 provider records after adding the separate two-hospital walkthrough.

A separate **Platform walkthrough** workspace now contains **Example Hospital**,
**Riverside Medical Center** and client **Jordan Lee**. It creates no agent assignments
or outreach. Existing user tasks were preserved.

Open: http://localhost:8000/?firm=a3e07a12-ce4f-589f-86d4-614185952329

## Journeys exercised

These eight journey types were verified through sequential real-model runs against
disposable databases. Reruns exposed the issues below; this is not an accuracy benchmark.

| Journey | Observed result |
|---|---|
| Two hospitals | Description becomes approved scopes; supervisor creates and starts separate specialists |
| Unknown hospital / unsupported delivery | Questions appear; no partial work starts |
| Bills arrive first | Bills remain received; reminder requests only records and imaging; final evidence completes the task |
| Contact hold | No contact under the hold; resolving it resumes the same task |
| Failure and manual contact | Injected failure preserves the saved send; recovery retains the assignment and manual facts; no duplicate send |
| Client concern | Sensitive self-report creates review; acknowledgement permits same-task completion |
| Older contact detail | A clear request retrieves and cites an old employee name among 45 newer updates, without that answer in initial context |
| Worker restart | Update is saved with the worker offline; a new worker resumes the same Temporal workflow and completes once |

Duplicate approval, duplicate receipt and duplicate recovery were included in these
journeys. Separate controlled tests exercise operations alerts, scope isolation,
context budgets and a third test-only domain adapter using the shared executor.

## Fixes made

1. **Visible evidence was not consistently accepted as evidence.** Receipt IDs shown in
   domain state could be rejected unless the model redundantly reread a tool. Adapters
   now declare visible evidence references; the runtime verifies their case scope.
   Records validation still checks saved requirement coverage before completion.
2. **An incorrect model proposal could become a technical execution failure.** Invalid
   citations and unproven receipt/completion claims now save the rejected proposal and
   provide correction feedback. Two automatic corrections are allowed before an actionable recovery incident
   offers retry/cancel, using the existing execution-recovery primitive.
   Successful logical actions remain saved, so replanning does not repeat a send.
3. **A lifetime activation cap could block useful long-running work.** The guard now
   counts automatic polling, and fresh business information resets it. A regression
   exercises ten provider updates. Reminder limits and contact restrictions still apply.
4. **Weak keyword matches could crowd out a strong semantic-only answer.** Retrieval
   now retains the strongest semantic candidate before filling the remaining ranked
   results. A deterministic regression recreates that failure with keyword noise.

5. **A premature action could erase an otherwise useful summary.** A NOT_DUE guard
   now returns a structured, audited tool result to the agent. The attempted operation
   rolls back, its logical send key stays available, and the agent can finish reporting
   the facts it already retrieved. Execution details show postponed actions.

6. **A ready plan could still omit an unsupported part of the description.** A separate
   model coverage review now compares every requested outcome with the proposed plan.
   An uncovered part yields clarification and no assignments. Old, unverified draft
   plans require a new review; already approved assignments keep their replay behavior.

The activity feed names rejected plans clearly. Operators receive a short explanation;
execution details retain the original rejected output and technical feedback.

## Important remaining gaps

- **Vague information goals remain fragile.** The model sometimes interpreted
  “designated representative” as the hospital itself and skipped historical search.
  Instructions now distinguish requested historical details from current entity
  details; a clear individual-person lookup passes. Broader wording reliability is
  unproven. Next, make information prerequisites explicit in the proposed plan and
  verify them before dependent outreach.
- **Retrieval needs a larger evaluation set.** Preserving a semantic candidate improves
  recall; it does not establish relevance or truth. Automatic task/provider scoping
  and contradictory hospital reports deserve more live evaluation.
- **Planning is bounded to registered capabilities.** A new domain reuses execution,
  waiting, evidence and recovery, but still needs intake schemas, tools, validation,
  routing and presentation. Arbitrary multi-stage case planning is not shipped.
- **Monthly consolidation is still a design proposal.** The existing digest is recent
  source excerpts, not a durable account of unresolved issues and commitments.

## Validation and practical limits

- Full regression run: **255 passed, 8 skipped**. The skips are the separately opted-in
  live-model journeys. All eight journey types were also verified with live model calls.
- Focused memory/context/replanning checks: **35 passed**. After the final escalation
  refinement, **22 recovery and client-review checks passed**. The final guard/routing
  checks passed **29 tests**, and the final live-model run passed **all 8 journeys**.
- Ruff, formatting, JavaScript syntax and diff checks passed.
- Outreach and documents are simulated. Failures and due dates were deliberately
  controlled; the tests did not actually wait several months.
- Tests use API requests and SDK/worker execution, including real Temporal persistence.
  They are not a complete browser click-through suite.

Repeat the live checks with:

```sh
RUN_INTEGRATION_TESTS=1 RUN_LIVE_AGENT_TESTS=1 .venv/bin/pytest -x -v tests/test_live_journeys.py
```

They require the local PostgreSQL/Temporal services and an OpenAI key, incur model API
cost, and clean up their temporary database. The walkthrough workspace can be recreated
idempotently with `examples/create_walkthrough.py`.

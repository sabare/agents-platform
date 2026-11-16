# Browser demo rehearsal

Date: 15 November 2026. Workspace: **Interview presentation**, CASE-101.
Real OpenAI calls and Temporal workers were used. All documents, calls and people
in this walkthrough are fictional; outreach is simulated.

## What completed

| Interviewer question | Observed result |
|---|---|
| Does “records attached” count without a file? | No. The message was preserved, but did not satisfy receipt. |
| What about 40 pages with missing surgical notes? | General records received; notes outstanding; authorization review opened. |
| Can a phone call introduce a restriction? | Yes. The saved transcript produced sourced protective memory and an Activity comment. Contact paused. |
| Does resolving one hold clear everything? | No. Resolving the call-review hold left authorization outstanding. |
| What happens if the worker is down during approval? | Approval persisted. Restart resumed the same task and applied one authorized follow-up for missing notes. |
| Does approval itself complete collection? | No. Completion waited for the surgical-notes attachment. |
| Does the supervisor see completion? | Yes. It verified the records objective as satisfied. |
| Can it substitute an unknown hospital? | The plan asked for clarification; no task or outreach was created. |
| Does a future check-in send early? | Scheduled 18:15:46 UTC; sent 18:15:48.270325 UTC. One simulated send. |
| What about a concerning client reply? | Preserved as an unverified self-report; human review required. The same task completed after acknowledgment. |
| Can we inspect model behavior? | Jaeger contains context, model, tool, database and result spans. Langfuse contains generation observations. Exported text is redacted. |

The records task is `06140d6c-70be-464a-a18b-23b461b4262b`.
The client task is `7002cf4e-f3c9-45c8-9fb8-5d8cf1c9544e`.
Both are **COMPLETED**. No database shortcut was used to mark them complete.

## Fixes found during rehearsal

- A generic follow-up form could appear alongside a specific authorization review,
  especially after restart. Specific source reviews now take precedence; backfill
  does not recreate the generic form or clear its memory obligation.
- Unsupported supervisor event citations immediately became business clarification.
  They now receive two bounded correction attempts, then a technical retry action.
  Evidence checks remain enforced and rejected proposals apply no actions.
- A lifetime supervisor counter could block new updates in an old case. Automatic
  decisions now have their own budget; fresh events remain processable.
- A live-model test exposed an unknown-hospital substitution that both planning
  and coverage models accepted. Records plans now require the selected provider
  name in the original request, or explicit all-provider scope. The backend checks
  again before approval. Unrecognized names and aliases require clarification.
  This is conservative name binding; the model still reviews omissions and negation.
- A live test found a ready plan that silently dropped PDF email delivery.
  Explicit email/web destinations must now appear in a registered agent's validated
  operational input; otherwise the plan asks for clarification with no steps.
  Summary disclaimers do not satisfy delivery. Other scope coverage still uses
  model review, so this does not prove complete understanding of arbitrary text.
- Invalid planner output now gets one bounded correction attempt within the
  shared 60-second planning deadline. Repeated invalid output becomes a saved
  clarification with no steps. Infrastructure failures remain errors, with their
  type logged. Failed parse-attempt usage may only be available in model traces.
- A sent check-in repeatedly polled the model without new information. It now
  waits for an event, so the reply signal resumes it without a timer loop or polling-limit review.
- A stale migrate image could prevent worker startup. API, worker and migrations
  now share one image. Source-only rebuilds reuse cached runtime dependencies.
- Old browser assets hid changes. HTML revalidates and changed assets have new URLs.
- Active reviews were buried in old forms. Previous reviews, resolved incidents,
  technical assessments and manual contact are folded away.
- Intake now shows the selected filename and queued-review status; review errors
  stay beside the form. Client reply controls appear in the task header. Parent
  tasks are clickable and long names wrap on narrow screens.

## Where to look

Open [the completed workspace](http://localhost:8000/?firm=e57bc555-d9d2-51dd-91fa-ea172894eabc).
Open either task → **Executions** → expand an activation → **Open execution trace**
or **Open LLM trace**. See [the presentation script](presentation-walkthrough.md)
for prompts, uploads, review actions, Temporal history and dashboard login guidance.

Proof files and screenshots are in the chat workspace under
`outputs/demo-rehearsal/`. `proof.json` records IDs, timing and trace checks.

A separate [Interview live workspace](http://localhost:8000/?firm=c51532c8-5015-53f6-81c3-c1c9e6f646c4)
is ready for the presentation, with no agent assignments started. The completed
workspace stays available as evidence. To prepare another clean run, use a new key:

```sh
.venv/bin/python examples/create_presentation_workspace.py --key interview-run-2 --name "Interview run 2"
```

## Limits to say clearly

This is evidence for these scenarios, not a general reliability score. Client
change comparison uses configured rules. Call/document interpretation uses a model
plus backend checks and can still require review. Contact and delivery are simulated.
The local stack uses a shared operator credential. Existing test workspaces and
historical incidents remain visible; they have not been deleted to clean up the demo.

## Validation

- Full regression run: **304 passed, 8 opt-in live tests skipped**.
- Additional affected-domain checks: **24 passed**, covering event waits and review/resume.
- Frontend: **19 passed**. Ruff, formatting and whitespace checks passed.
- Live interviewer run: seven scenarios passed; the remaining ambiguity/delivery
  scenario exposed intake issues described above and was rerun after their fixes.
- Final intake regression checks: **36 passed**. The previously failing live
  ambiguity/delivery test passed after the fixes, completing coverage of all eight
  live scenarios across the run and targeted rerun.
- The browser rehearsal completed both workflows and verified Jaeger/Langfuse data.

All live tests use disposable databases and simulated outreach. They do not add test
firms to the presentation workspace. The clean workspace remains unassigned.


## Walkthrough recheck — 16 November 2026, IST

The short presentation guide now explicitly includes the second-agent prompt,
parent-to-child navigation, the scheduled send, simulated client concern and final
acknowledgment. The matching `START-HERE.md` was refreshed.

Current checks used disposable databases, leaving Interview live unchanged:

- Live OpenAI/Temporal hospital journey: all **13 behavior checks passed**.
- Client review with a real model, scheduled send, and restart tests: **3 passed**.
- Combined records walkthrough plus approval tests: **13 passed**.
- Registry/extensibility and demo regressions: **24 passed**.
- Frontend checks: **20 passed**; lint and formatting passed.

The combined sequence exposed one timing bug: an authorized continuation could
send before an existing callback. It now honors the later saved callback/commitment.
The new sequence test covers early final documents completing the task without
waiting days or sending an unnecessary follow-up. The live hospital verification
script was updated to the current Luna agent version.

Assessment: strong working demo for reusable runtime, waiting, evidence and human
review. Keep the presentation on the two workflows, then show one trace and agent
registration. Outreach is simulated, assumptions favor the fictional demo, and
model interpretation/latency remain variable. These checks do not establish broad
production reliability. No fresh browser walkthrough was possible in this pass;
UI controls were checked in code and frontend tests, and flows ran through APIs,
actual models and Temporal.

## Fresh frontend pass — 16 November 2026, IST

Completed both exact prompts from `presentation-walkthrough.md` in Chrome, using
**Frontend walkthrough check** (`80df803b-81c4-595b-845e-943be7a300c8`).
Interview live was left unchanged.

- Records task `7caecc2c-ebdd-48ef-89bc-0c747a1c3abb`: initial request,
  exact three-day callback, general-records PDF, authorization upload/review,
  and surgical-notes PDF all submitted through the frontend. **Completed**,
  both requirements received, zero reminders. Approval honored the callback;
  early final delivery completed without waiting three days.
- Client task `c8763489-8a60-425b-b073-b70718c4d907`: exact two-minute prompt
  produced an approvable plan. Actual timer sent one simulated check-in.
  The exact concerning reply opened review; the walkthrough acknowledgment
  resumed and completed the same task. Saved tool history confirms one send.
- The records execution link opened Jaeger with context, model, tool, database
  and Temporal spans. Final Activity includes both human decisions and agent
  completion summaries. Common audit events are persisted for both tasks.

Fixed during this pass: background refresh could replace an authorization file
input while its native picker was open. Focused review forms now remain intact;
selected files, deadline and attestation survive background updates. The parent
label now uses its actual supervisor version. Scheduled wake displays the current
reminder or authorized continuation time instead of an older execution time.

Validation: 21 frontend checks passed, including deadline/attestation restoration;
the combined records walkthrough integration regression passed. Proof and final
frontend screenshots are in `outputs/frontend-recheck/` in the chat workspace.
Only the intended authorization and concerning-client reviews appeared. This
checks the two demo journeys; model latency and interpretation can still vary.

## Submission checks — 16 November 2026

- `RUN_INTEGRATION_TESTS=1 .venv/bin/pytest -q`: 328 passed, 8 opt-in live
  tests skipped; one existing activity-feed assertion still expected an empty
  result instead of the scoped resource lookup's 404 response.
- Updated that assertion to check identical 404 responses for foreign and unknown
  task IDs. Reran the affected module: **23 passed**. No runtime change was needed.
- `node --test tests/frontend/*.test.cjs`: **21 passed**.
- Ruff lint, formatting, JavaScript syntax and Git whitespace checks passed.
- The installed `.agents` skill bundle is excluded from application Ruff checks;
  its scripts are external developer tooling. Application and test code remain checked.

See `INTERVIEW-NOTE.md` for the submission's design explanation and explicit limits.

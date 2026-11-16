# Reported information from calls and messages

Open an active specialist task and expand **Simulate call**. Select a case contact,
paste the transcript and save. No phone call is placed.

Try: “We found the records. Do not call about this request until you receive an
updated authorization. Please provide an updated authorization for this request.”

Watch Activity for the simulated call and its agent comment. Open the original
transcript from the comment. Memory shows sourced facts and blockers. Human review
appears on the task. A medical authorization prerequisite requires a reviewed,
uploaded authorization document. Recording the decision wakes the same task.

## How it works

1. An authenticated operator submits the transcript. `demo_call()` wraps the shared
   ingestion function; `/communications` also accepts `EMAIL` and `MESSAGE`.
2. PostgreSQL saves the original text, contact, channel, timezone and reported time
   as a Source, together with an Activity event, inbox item and outbox wake.
3. The worker runs a separate, tool-free LLM interpreter. It proposes facts,
   commitments, restrictions, prerequisites or policy changes with exact quotes.
4. Code checks quotes, dates and scope. A transaction saves the interpretation,
   memory, a source-linked agent comment and any human request. Retries reuse the
   source key, so one input produces one saved interpretation and comment.
5. The worker assembles fresh context. Current blockers gate contact and completion.
   Human review resolves only memory from that interpretation, records its reason
   and sources, and resumes through the existing outbox/Temporal path.

The Activity comment is assembled from validated quotes and category labels. It is
attributed to `communication_interpreter/1`, and clearly says the call was simulated.
This avoids adding unsupported details from a second model-written summary.

## Who decides what becomes memory?

| Proposed information | Backend treatment |
|---|---|
| Task fact | Optional NOTE, labelled reported speech |
| Dated task commitment | NOTE with due time; retained in mandatory context |
| Contact restriction | STOP_CONTACT; review required |
| Prerequisite or requested policy change | OBLIGATION; review required |
| Broader/unknown scope, unsupported quotes, unclear dates, classifier failure | Review obligation; contact and completion pause |

The model cannot publish firm instructions, edit contacts, clear old holds, or mark
records received. A new number remains reported information for review; routing is
not changed. Reported facts are not independently verified facts. A quotation proves
what the transcript says, not that the speaker has authority or is correctly identified.

A commitment must contain an explicit calendar date with year. Date-only promises
use **09:00 in the submitted timezone** as the platform's follow-up convention.
Relative dates such as “Friday” require review. Contact is gated before that time.
The medical adapter also updates its reminder schedule. PostgreSQL stores the due
memory and domain schedule; Temporal owns the durable wait. Other incoming events
can wake the task early, but contact gates still enforce the due time.

A promise does not satisfy a records requirement. Only the existing receipt/evidence
path can do that. An uploaded authorization is checked for task/case/firm ownership;
the operator attests validity. There is no signature or legal-validity verification.

## Test and limits

Run from the repository root:

```sh
RUN_INTEGRATION_TESTS=1 pytest tests/test_communications.py -q
node --test tests/frontend/*.test.cjs
python examples/communication_demo.py --live --output work/communication-demo
```

The demo uses real Temporal, a disposable PostgreSQL database, and OpenAI when
`--live` is supplied. Without it, the classifier is an explicitly labelled regex
baseline. The demo checks source preservation, grounded categories, protective
memory, one comment, human review, worker restart and a dated promise. It removes
its temporary workflows/database afterwards; it does not add permanent workspaces.

This is a bounded text-ingestion slice: 10,000 characters and at most 12 claims per
input, one model turn, 2,500 output tokens and a 45-second timeout. No audio, caller
verification, actual email ingestion, automatic number changes or broad semantic
accuracy benchmark. Exact-quote checks cannot prove a classification is correct.
Ambiguity is sent to review; a small restriction-cue detector provides an additional
fallback, not complete protection against missed restrictions.

The original Source and interpretation keep separate identities. The interpretation
stores the classifier version, prompt snapshot/hash, model and trace reference.
Generic memory editing cannot bypass its typed review. A resolution records its
source and reason in memory history and the next task context, so the old analysis
is not mistaken for a still-open requirement. Existing audit and redacted tracing
cover the API, source writes, model calls, human decision and subsequent tools.

# Hospital response: behavior demonstration

The example response is:

> We have attached 40 pages of records. However, the surgical notes are unavailable.
> Please provide an updated authorization.

The task requests **general records and surgical notes as separate requirements**.
A large attachment does not establish that both arrived.

## Implemented flow

| Step | What happens |
|---|---|
| 1. Receive | `POST /v1/firms/{firm}/tasks/{task}/provider-responses` saves the original message and attachment bytes in a `PROVIDER_RESPONSE` source. Exact retries reuse it; conflicting retries return 409. |
| 2. Wake | The same transaction saves an event, task inbox entry and Temporal wake outbox message. The API returns 202; it does not hold a database lock during model work. |
| 3. Extract | The records adapter's ingestion hook reads text-based PDF/text files, counts actual pages and hashes the bytes. In live mode, a structured LLM call identifies receipts, missing items and authorization needs. |
| 4. Check | Backend code checks requirement IDs, attachment/page references and exact quotations. A statement that files were attached is insufficient. Unsupported citations and unreadable documents require review. |
| 5. Remember | Accepted documents get source-linked `RecordEvidence`. General records become received; surgical notes stay outstanding. A task-memory obligation records the authorization blocker; case memory and supervisor inbox are updated. |
| 6. Stop safely | The worker sees current memory and proposes a next step. Backend gates block contact and completion while the obligation is open. A typed human request links the original response and extraction. |
| 7. Decide | The teammate uploads an updated authorization, attests that they reviewed it, and chooses a response deadline. The document must belong to this task. A comment, generic memory resolution or RECHECK cannot substitute for this decision. |
| 8. Resume | The saved decision resolves that blocker and permits one simulated follow-up with the authorization reference. Existing holds and current provider state are checked again. Only the surgical notes are requested. |
| 9. Finish | Approval alone leaves the task waiting. A later surgical-notes document must arrive before completion. The supervisor verifies the completed child and its evidence before satisfying the objective. |

Original sources are not edited by extraction. The derived analysis stores the extractor
version, prompt snapshot/hash, model, citations and validation errors. Attachment bytes
stay out of model/search text projections; selected extracted text is used instead.

The existing audit journal records ingestion events, authorization decisions, human
approval and the simulated follow-up effect. OpenTelemetry/Langfuse link the model calls,
tools and execution outcomes. The model's proposed action is saved **before** backend
validation in `Execution.usage_json.agent_proposed_action`.

## Run the repeatable demonstration

With PostgreSQL, Temporal and local observability running:

```sh
python examples/hospital_response_demo.py
python examples/hospital_response_demo.py --live --output work/hospital-scenario-live
```

The first command uses the labelled offline baseline. `--live` makes actual OpenAI calls
and incurs usage. Both use real Temporal workflows and a disposable PostgreSQL database.
They stop the worker while blocked, save the human decision, restart the worker, and check
resumption. They leave no extra workspace in the application.

The output directory contains the fictional 40-page PDF, updated authorization text,
2-page surgical-notes PDF and `results.json`. The report checks:

- Original message/bytes, actual page count and correct partial coverage.
- Extractor recognition of the missing notes and authorization.
- Whether the agent itself proposed `needs_human` before the backend intervened.
- Typed human review, worker restart and follow-up limited to missing notes.
- No completion on approval alone; completion and supervisor verification after delivery.
- Audit coverage and hash-chain integrity.

A false check fails the demonstration. The report also shows the extractor output,
observed coverage, model-proposed action, backend status and trace links.

## Try it in the frontend

1. Create a **Medical Records Agent** task with **Records** and **Surgical notes** checked.
   Uncheck **Imaging** and **Bills** for this scenario.
2. Open the task, choose **Simulate provider response**, and keep **Read response and
   attached documents** selected. Paste the message above and upload the generated 40-page PDF.
3. Wait for **Records / Received**, **Surgical notes / Outstanding**, and the blocked
   human request. Inspect its supporting evidence and the Executions trace links.
4. In the human request, upload `updated-authorization.txt`, check the review attestation,
   choose a future response deadline and record the decision.
5. Confirm the task resumes and waits. Submit the 2-page surgical-notes PDF with a new
   response saying the requested surgical notes are attached. The task can then complete.

The automatic demo includes supervisor delegation and verification. It uses the registry's
`approved_assignments` path, with exact approved inputs. New records tasks and supervisor objectives use version 4; existing pinned definitions are retained.

## What the live checks taught us

An older supervisor path proposed the wrong delegation action for a legacy target list.
The backend rejected it. Live extraction also initially inferred that previously received
records were missing from a later delivery. Its invented quotation was rejected, leaving
work blocked. Extraction version 2 now receives outstanding requirements separately from
prior coverage and explicitly forbids inferred quotations.

The worker initially proposed waiting on authorization; the backend blocked it. Records-agent
version 4 makes that prerequisite require human escalation. The supervisor also confused
source IDs with event IDs. Supervisor version 4 makes citation types
and child-task review ownership explicit. State-derived evidence questions are retired when current coverage verifies completion;
this does not resolve authorization reviews. These are observed failures and tested fixes,
not evidence that arbitrary planning is now reliable.

## Limits

Fixtures are labelled, fictional documents. The offline baseline recognizes narrow document
labels and phrases; it is not presented as model reasoning. Live mode uses an actual model,
with code enforcing the safety boundaries. This scenario is not a broad quality benchmark.

PDF support is bounded: at most three 2 MB files, at most 100 pages per file, bounded text
and a model input budget. Scanned, encrypted, malformed, ambiguous or unsupported date-scope
inputs go to review. There is no OCR, clinical completeness assessment, patient-identity
matching, signature validation or real hospital delivery. The operator attestation is the
human authority; the model cannot create it. Production parsing needs isolated resource
limits, object storage, authenticated provider callbacks and delivery reconciliation.

Tests in `tests/test_provider_response_scenario.py` cover the story, unsupported citations,
missing/unreadable attachments, repeat delivery, blocked contact, attempted false completion,
typed approval, protected memory and binary-free model/search projections.

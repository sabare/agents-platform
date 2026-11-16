# Platform for Long-Running Agents

## Design and scope

I built a working slice for medical records collection and client check-ins, with a
shared runtime rather than a separate execution engine for each workflow.

The main design choice is to separate three responsibilities:

- **PostgreSQL stores what is true now:** assignments, progress, evidence, rules,
  human decisions and saved executions.
- **Temporal manages when work runs:** durable timers, incoming signals, activity
  retries and resumption after a worker restart.
- **The agent reasons for one bounded turn:** it receives current context, calls
  permitted tools and proposes a structured next step.

A task can last months without keeping a model conversation or process alive.
The system rebuilds context when it wakes. It can also return to waiting without
calling the model when backend state already shows that an action is not due.

The implementation is a modular Python application using FastAPI, the OpenAI
Agents SDK, SQLAlchemy and PostgreSQL. API and worker processes share domain code.
I chose this structure to keep transaction boundaries and debugging manageable
while testing the platform abstractions. Separating these modules into services
would add operational work before there is evidence that they need independent scaling.

## Product and platform primitives

| Primitive | Purpose |
|---|---|
| Task | A persistent responsibility with an objective, scope, state and pinned agent version. Parent tasks group specialist work. |
| Execution | One activation, with its saved context, decision, tool history, usage and committed result. |
| Agent capability | A developer-owned definition, typed assignment contract and domain adapter that the supervisor can discover. |
| Source and evidence | Original information plus explicit links supporting claims, document receipts and decisions. |
| Memory | Current rules, restrictions and obligations, separated from searchable historical information. |
| Human request and decision | A specific blocker, recorded response and controlled continuation on the same task. |
| Event, inbox and outbox | Business changes, pending input and retryable delivery to Temporal. |
| Audit and traces | Durable records of actions and authority, alongside diagnostic execution traces. |

The supervisor works from an operator-approved objective and the available
capability catalog. It proposes specialist assignments and checks progress when
new events arrive. Specialist agents own their domain work. The backend validates
scope and completion; a supervisor summary alone cannot mark records received.

## How execution and waiting work

An activation follows a **prepare → decide → commit** sequence:

1. Load the tenant-scoped task, pinned definition, domain state, current memory
   and pending events. Save the expected versions and execution context.
2. Run a bounded model/tool loop with typed inputs and structured output.
3. Recheck freshness, evidence and domain rules before saving the next state.

No database transaction stays open while waiting for a model response. Tools use
short transactions and recheck permissions and current restrictions before acting.
Successful tool actions are saved before the final agent decision. If that decision
fails validation, previously committed actions remain recorded and deduplicated.

A waiting outcome can specify a wake time or wait for a new event. Temporal holds
the timer. A provider reply, client response or human decision can signal the
workflow earlier. On waking, the worker reloads current state: if the required
records have already arrived, it completes instead of sending the scheduled reminder.

Task changes and wake requests are written together in a PostgreSQL transaction.
A dispatcher delivers outbox rows to Temporal and marks them delivered. A crash
after delivery but before acknowledgment can cause another delivery attempt, so
workflow identities, inbox handling and logical tool-action keys make repeats safe.
This is at-least-once delivery with application deduplication, not a blanket claim
of exactly-once execution. Real external delivery would need provider-specific
idempotency and reconciliation as well.

## Memory, context and changing information

The platform does not send the whole case history to every agent.

**Current operational memory** includes approved instructions, contact restrictions,
open obligations and relevant task state. These are loaded directly through scoped
queries. Their inclusion does not depend on semantic search. Fingerprints and
revisions let the backend detect changes before an action or final commit.

**Historical evidence** is stored as sources, events and derived passages. Agents
can use `retrieve_context` for focused questions and `read_evidence` to expand a
source. Retrieval supports exact, keyword and optional vector search, with tenant,
case and applicable provider/date filters. Source hashes help reject stale derived
content. Keyword search remains available if embeddings fail.

Context has explicit limits: the default model input budget is 12,000 tokens,
output budget is 1,500 tokens and retrieval-call budget is four. Agent definitions
also bound turns, tools and execution time. Optional history and long excerpts can
be shortened while retaining source references. Mandatory controls are preserved;
if essential context still cannot fit, execution stops with a technical incident
rather than silently dropping a restriction.

Call transcripts use the same source-first approach. A model extracts reported
facts, commitments, restrictions or review needs; backend checks validate the
interpretation before applying memory changes. The transcript and a short Activity
summary remain available. Reported speech does not become approved firm policy
simply because a model classified it.

## Demonstrated behavior

The medical-records demonstration exercises more than a successful tool call:

1. The agent sends a simulated initial request and waits.
2. A simulated call says to call back in three days. The sourced commitment moves
   follow-up timing without creating an unnecessary human approval.
3. A provider response supplies general records but says surgical notes are missing
   and requests updated authorization. The original response and uploaded documents
   are preserved. Extraction links receipt to specific requirements and evidence.
4. General records become received; surgical notes remain outstanding. A specific
   authorization review blocks further contact and completion.
5. A teammate uploads and confirms the authorization. The same task resumes, while
   honoring the existing callback time. Approval does not count as receipt.
6. A later surgical-notes upload satisfies the remaining requirement. The task
   completes and the supervisor reports the result.

The second demonstration schedules one client check-in two minutes ahead. After
the durable timer sends it, a simulated reply reports worsening pain and asks for
help. The platform preserves this as an unverified client self-report, records the
change and opens teammate review. Acknowledgment resumes and completes the same
task. Saved tool history confirms one check-in send.

Both journeys were exercised through the frontend with real model calls and
Temporal workers. Automated tests also cover retries, stale state, evidence
validation, review/resume, context limits and capability discovery. These are
scenario-level checks, not a general measure of agent quality.

## Audit and observability

OpenTelemetry correlates case, task and execution spans with context assembly,
model calls, tools, database operations and result commits. Trace context passes
through the outbox and Temporal headers. Jaeger shows platform timing; local
Langfuse shows model observations, prompt/definition identifiers, token usage,
estimated cost and backend validation/completion scores.

Those scores describe backend outcomes, not whether every model interpretation is
correct. Readable model input/output is an explicit local-debug option; the normal
export mode redacts free text. Telemetry export is best effort and can lose data.

A separate PostgreSQL audit journal records a common actor/action/resource/outcome
contract, including authorization decisions, tool attempts, failures, approvals and
simulated effects. Business capture commits with the underlying transaction;
attempt/failure records use separate short transactions so rollback does not erase
them. Runtime database roles cannot directly rewrite the journal. Per-tenant hash
chains, retention controls and checkpoint verification provide tamper evidence.
A database administrator can still rewrite the chain; independent archival and
signed checkpoints are not implemented.

## Implemented versus simulated

**Implemented:** persistent tasks and executions; supervisor delegation; versioned
agent definitions; durable timers and signals; typed tools and domain validation;
source-linked PDF/text intake; transcript interpretation; scoped memory and
retrieval; human review and recovery; frontend controls; audit and tracing.

**Simulated or limited:** provider/client outreach, phone calls and incoming
communications are supplied through demo controls. There is no real email, SMS,
telephony or hospital integration. Text extraction supports suitable PDFs/text,
not a complete OCR pipeline. The deployment uses a shared operator credential and
local infrastructure, rather than production user identities or high availability.

The optional fictional-demo policy makes disclosed assumptions for routine timing
and wording. It is disabled by default and is not a substitute for production
policy. Authorization and contact restrictions still apply in the demo.

## Adding another use case

For an insurance follow-up agent, a developer would add:

1. A typed assignment contract and versioned agent definition containing its
   instructions, model, allowed tools, output schema and limits.
2. A domain adapter that loads claim state, implements checked tools and validates
   progress, evidence and completion. Extra tables or review handlers are added only
   where the use case needs them.
3. An `AgentCapability` in an installed `agent_plugins` module's `CAPABILITIES` list.
   `expose_to_supervisor` defaults to `True`.

Startup scans developer-owned modules and publishes definitions. The supervisor
reads the published capability catalog and receives typed delegation tools. It does
not need an insurance-specific routing branch. Discovery requires a normal code
deployment/restart; it does not execute arbitrary user-uploaded agent code.

The new agent reuses task execution, waiting, context, reviews, audit and tracing.
Published definitions are stored as immutable snapshots with hashes; existing tasks
remain pinned to their assigned versions. This preserves configuration history,
not old Python executables: compatible adapter code must remain deployed.

## Current weaknesses and next steps

The largest remaining gap is evidence of consistently good decisions across varied
inputs. The backend rejects invalid actions, but it cannot prove that a plausible
extraction or plan understood every important detail. Retrieval can miss relevant
history, conflicting information can need review, and model latency varies.

Other limits include small upload sizes, basic PDF extraction, a single shared
operator identity, no real-delivery reconciliation and a local deployment without
load or disaster-recovery validation. The per-tenant audit chain also serializes
appends and would need measurement under higher volume.

With more time, I would prioritize:

1. **Behavior evaluation:** labelled planning, extraction, retrieval and escalation
   cases; repeated runs; failure analysis; release thresholds tied to outcomes.
2. **One real communication channel:** durable send intents, verified callbacks,
   delivery receipts and reconciliation of unknown outcomes.
3. **Production boundaries:** user/firm authorization, storage and retention policy,
   independent audit archival, backup/restore drills and deployment compatibility.
4. **Scale based on measurement:** long-case retrieval tests, spend limits, queue
   fairness, database indexes and worker capacity.

The aim is to make new workflows reuse reliable platform behavior while keeping
business-specific evidence and completion rules explicit in their adapters.

---

[Architecture and implementation](ARCHITECTURE.md) ·
[Demo walkthrough](presentation-walkthrough.md) ·
[Rehearsal evidence and limits](demo-rehearsal.md) ·
[Adding an agent](adding-agents.md)

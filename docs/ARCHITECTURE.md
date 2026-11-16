  # HelloCounsel Agent Platform: Architecture and Implementation

  **Scope reviewed:** repository implementation, including observability, audit, provider-response and communication paths · **Updated:** 16 November 2026

  **Scope:** the running API/worker path, database models and migrations, tests and demos.
  This describes the implementation. Proposed improvements are labelled separately.
  Path convention: package files such as `execution.py` are under `src/agents_platform/`;
  other paths start at the repository root.

  ## 1. Executive summary

  This platform runs assignments that span many short agent executions: collect records,
  wait for a provider, check again, accept new evidence, or ask a teammate for help.
  Medical-records collection and client check-ins share the same task runtime.

  The central rule is: **PostgreSQL remembers the work; Temporal remembers when to wake
  it; an agent acts for one bounded turn.** A long-running task is not a continuously
  running model conversation.

  This is internal AI infrastructure with a small operator UI. Its main abstractions
  are reusable assignments, versioned agents, checked tools, saved evidence, durable
  waiting and human decisions. It is not a full case-management product.

  Implemented paths include supervisor delegation, automatic discovery of registered
  agents, timed follow-ups, partial receipts, current and historical memory, human
  review, conversation-to-memory ingestion, failure recovery, a shared audit journal
  and distributed tracing. Provider contact, client contact
  and document receipt are **simulated**. Production authentication, real delivery and
  model-quality evaluation remain incomplete.

  **Reading guide:** architecture (§3), runtime (§5–6), memory (§7–8), audit (§11),
  workflow example (§16), extension guide (§17), implementation status (§18).

  ## 2. Repository and entry points

  | Location | Role |
  |---|---|
  | `src/agents_platform/api.py` | FastAPI app, route wiring, request logs and health checks |
  | `orchestration/worker.py` within that package | Publishes definitions; starts Temporal activities, workflows and dispatcher |
  | `coordination.py`, `execution.py` | Supervisor and worker prepare/decide/commit paths |
  | `capabilities.py`, `delegation.py`, `agent_plugins/`, `definitions/` | Agent discovery, contracts and versioned configuration |
  | `models.py`, `migrations/versions/` | Persisted state and Alembic schema changes |
  | `audit.py`, `audit_api.py`, `audit_integrity.py`, `audit_roles.py` | Common audit events, journal reads, verification and restricted database access |
  | `observability.py`, `telemetry.py`, `operations.py` | OpenTelemetry/Langfuse spans, JSON logs and operational signals |
  | `provider_responses.py`, `source_content.py` | Source-first document intake, cited extraction, authorization blockers and binary-free text projections |
  | `communications.py` | Shared transcript intake, grounded classification, sourced memory, Activity comments and typed review |
  | `static/` | Plain HTML, CSS and JavaScript operator UI |
  | `tests/`, `examples/` | Automated checks and repeatable demonstrations |

  Host start points are `uvicorn agents_platform.api:app`, `agents-worker` and `agents-seed`.
  Compose API, worker and migrate use the same `agents-platform-app:local` image.
  The image loads `/app/src` through `PYTHONPATH`; its worker starts with
  `python -m agents_platform.orchestration.worker`. Runtime dependencies are cached
  before source is copied, so a code change does not reinstall the application.
  `cli.py` and `supervisor.py` retain the earlier inline, analysis-only scaffold; they
  are not the durable product path. Migrations cover the task foundation, executions,
  coordination, memory, schedules, reviews, partial follow-up state, recovery and the audit journal.

  ## 3. System architecture

  The following groups describe responsibilities inside **one modular application**.
  They are not separate microservices or separately deployed audit/tool services.

  ```mermaid
  flowchart TB
    subgraph CP[Control plane: define and assign work]
      UI[Operator UI / API]
      REG[Registry and published definitions]
      INTAKE[Assignment planner and approval]
      SUP[Case supervisor]
      HUMAN[Human requests and decisions]
      UI --> INTAKE
      UI --> HUMAN
      REG --> SUP
      INTAKE --> SUP
    end
    subgraph EP[Execution plane: run and resume work]
      TM[Task management]
      DISP[Outbox dispatcher]
      TEMP[Temporal workflows and timers]
      RT[Shared worker runtime]
      CTX[Context assembly]
      AG[Worker agents / model]
      TOOL[Checked tool callbacks and domain adapters]
      SUP --> TM
      TM --> DISP --> TEMP --> RT
      RT --> CTX --> AG --> TOOL
      HUMAN --> TM
      TOOL --> RT
    end
    subgraph XI[Shared infrastructure]
      DB[(PostgreSQL: state, memory, evidence)]
      EVT[Events, inbox and outbox]
      AUD[Common audit journal: decisions, attempts, effects]
      OBS[OpenTelemetry spans, JSON logs and operations monitor]
      JAEGER[Local Jaeger]
      LANGFUSE[Local Langfuse]
      OBS --> JAEGER
      OBS --> LANGFUSE
      DB --- EVT
      DB --- AUD
      EVT --> SUP
    end
    REG --> DB
    TM --> DB
    CTX --> DB
    TOOL --> DB
    RT --> AUD
    RT --> OBS
    DISP --> OBS
    HUMAN --> AUD
  ```

  The model proposes actions. Backend code validates scope, evidence and current rules.
  Temporal controls waiting and retries; it does not decide whether records are complete.
  Events notify participants; database rows hold current business state.

  | Technology | Use and practical reason | Limit |
  |---|---|---|
  | Python, FastAPI, Pydantic | One language for API, typed contracts and agent tools | Shared process/code trust boundary |
  | OpenAI Agents SDK | Model/tool loop and typed agent output | Application still owns persistence and validation |
  | PostgreSQL, SQLAlchemy, Alembic | Transactions, constraints, evidence and schema changes | Requires careful locking and migrations |
  | pgvector + PostgreSQL full-text search | Optional semantic search beside keyword search | Search is derived data, not business authority |
  | Temporal | Durable timers, signals, activity retries and workflow history | Additional service and deployment compatibility work |
  | OpenTelemetry, Jaeger, Langfuse | Correlated platform spans and redacted model/tool traces | Best-effort export; local Jaeger storage is in memory |
  | tiktoken | Estimate and bound model input | Estimates include a reserve; not provider billing truth |
  | Docker Compose, plain browser UI | Small local stack that is easy to demonstrate | Development deployment, not a highly available production setup |

  These reasons follow from the implemented boundaries; they are not claims that each
  alternative was benchmarked. `compose.yaml` uses PostgreSQL 17 and Temporal's development
  server with a persistent local database file. Temporal history is separate from the
  application's PostgreSQL data.

  ## 4. Core model and versioning

  | Entity | Meaning and separation |
  |---|---|
  | `AgentVersion` | Published JSON snapshot: instructions, model, allowed domain tools, output schema, limits and hash |
  | `Task` | Long-running responsibility: firm, case, objective, typed input, assigned agent/version and current progress |
  | `Execution` | One saved activation: context, proposed decision, committed outcome, usage, attempt and error |
  | `CaseCoordination` | One coordinator per case: objective, plan, supervisor version, revision and workflow identity |
  | `Source`, `RecordEvidence` | Original attributed information, plus links proving particular records requirements |
  | `ActivityEvent`, `TaskInbox`, `OutboxMessage` | Business changes, unread work and retryable delivery requests |
  | `HumanRequest`, `HumanDecision`, `HumanContinuation` | A question, a recorded response and any specific authorized continuation |
  | `ExecutionIncident`, `ManualContact` | Technical failure recovery and a person's account of work outside the app |

  ```mermaid
  erDiagram
    CASE ||--o{ TASK : contains
    CASE ||--o| CASE_COORDINATION : has
    AGENT_VERSION ||--o{ TASK : assigned_to
    TASK ||--o{ EXECUTION : activates
    TASK ||--o{ TASK : groups
    TASK ||--o{ ACTIVITY_EVENT : records
    EXECUTION ||--o{ TOOL_INVOCATION : produces
    TASK ||--o{ HUMAN_REQUEST : blocks_on
    HUMAN_REQUEST ||--o{ HUMAN_DECISION : receives
    SOURCE ||--o{ RECORD_EVIDENCE : supports
  ```

  Agent versions are stored under `(agent_key, version)` in `agent_versions`. Publication
  in `registry.py` inserts new snapshots and rejects edits to an existing version. Tasks
  reference the exact version. New or explicitly revised objectives use the current deployed supervisor definition
  (v5 in the demo); existing objectives retain their saved version.

  Input/output schema names and adapter versions provide separate compatibility checks.
  A task's numeric `version` detects concurrent state changes; it is not an agent release.
  Saved context also records definition hashes, adapter identity and memory revisions.
  There is no universal, separately versioned context-packet contract.

  **Limit:** the database preserves configuration, not old Python executables. Compatible
  tools/adapters must remain deployed. Parent tasks group work; there is no general
  engine for general graphs of dependent tasks. See `models.py`, `domains.py`, `registry.py`.

  ## 5. Runtime: one execution

  `execution.py` adds application state and safeguards around the Agents SDK. Its actual
  entry point is:

  ```python
  run = await self._prepare(activation)
  if isinstance(run, dict):
      return run  # Previously committed result, closed task, or duplicate signal.
  decision, usage = await self._decide(run, self._tools(run))
  ```

  1. A workflow activity receives firm ID, task ID, cycle and trigger.
  2. **Prepare:** load the scoped task, reject old recovery generations, and return an
     already committed outcome when present. Resolve the pinned definition and adapter.
  3. Load current domain state, mandatory memory, unread events, recent events and history.
     Save the execution context and expected task version. A future due date can return
     `WAITING` immediately, without a model call or new execution row.
  4. **Decide:** construct an SDK `Agent` with permitted tools and the adapter's decision
     type. `Runner.run` performs model/tool turns. Scripted mode uses the same SDK path
     with deterministic responses.
  5. Each tool call validates input, reloads current state and policy, performs a checked
     action in its own short transaction, and returns saved results/evidence to the model.
  6. The SDK returns structured output. **Commit** checks task/memory freshness, known
     source IDs, open obligations and domain-specific completion rules.
  7. Persist the decision, outcome, usage, progress and events. Return a next step to the
     workflow; the model turn ends.

  ```mermaid
  sequenceDiagram
    participant W as Temporal workflow
    participant R as Runtime
    participant D as PostgreSQL
    participant M as SDK / model
    participant T as Checked tools
    W->>R: execute_task(task, cycle, trigger)
    R->>D: Prepare saved context and expected versions
    R->>M: Context + allowed tools + output schema
    loop Bounded model/tool turns
      M->>T: Typed tool call
      T->>D: Recheck scope/policy; save effect and result
      T-->>M: Result with source references
    end
    M-->>R: Structured decision
    R->>D: Validate freshness/evidence; commit outcome and events
    R-->>W: WAITING + wake_at / BLOCKED / terminal / CONTINUE
  ```

  No database lock spans a model call. Successful tools commit before the final decision;
  a later invalid decision does not undo those earlier actions. Stale work is refreshed.
  Evidence-validation failures get bounded correction attempts; repeated rejection
  creates a failure incident.
  The supervisor gives an unsupported event citation two correction attempts, with
  valid event IDs in feedback. No proposed action is applied on rejection. Repeated
  citation failure or a context overflow creates a technical review offering
  `RETRY_SUPERVISOR`; it does not ask the user to change the business objective.

  Worker budgets include definition turn/tool limits and a saved deadline. Model output
  has a token cap; `ModelBudget` checks each input, including schemas. The supervisor
  also has a three-action proposal limit and a 30-decision automatic budget. This
  budget counts automatic decisions, not lifetime history; new business events can
  still be processed after a long-running case accumulates many decisions. These are
  execution safeguards, not a complete spend-control or API rate-limiting system.

  ## 6. Long-running orchestration

  A sent client check-in with no reply returns `WAITING` with `wait_for_event`,
  without a `wake_at`. Temporal waits for a durable signal instead of polling the
  model. Its initial scheduled send still uses `wake_at` and a timer.

  Three lifetimes are separate:

  - **Task:** the business assignment, potentially lasting weeks.
  - **Execution:** a short activation; several attempts can reuse its saved context.
  - **Workflow:** durable control flow that starts activities, waits, and resumes them.
    A task can get a new workflow identity during explicit recovery.

  ```mermaid
  stateDiagram-v2
    [*] --> PENDING
    PENDING --> RUNNING: activation
    RUNNING --> WAITING: future date or pending reply
    WAITING --> RUNNING: timer or saved event
    RUNNING --> BLOCKED: human input required
    BLOCKED --> RUNNING: permitted update or decision
    RUNNING --> COMPLETED: validated evidence
    RUNNING --> FAILED: retries or validation budget exhausted
    FAILED --> PENDING: explicit recovery with new generation
    PENDING --> CANCELLED: permitted cancellation
    COMPLETED --> [*]
    CANCELLED --> [*]
  ```

  `TaskWorkflow` in `orchestration/workflows.py` runs `execute_task`, then uses
  `workflow.wait_condition` with a timeout for `wake_at`, or waits for a signal when
  blocked. Signals mean “recheck saved work”; they do not carry the authoritative facts.
  Every 100 cycles the task workflow continues as new to bound its history.

  Task activities have a five-minute attempt timeout, a six-minute overall scheduling
  window and at most three attempts. Replay reuses recorded workflow results; retries
  may repeat activity code. Stable execution IDs and saved tool keys prevent repeating
  committed simulated effects. A worker outage delays execution; it does not erase the
  timer. Availability at the exact due time is not guaranteed.

  Explicit schedules are domain rows: `ClientCheckIn.scheduled_at`, records callbacks
  and reminder dates, interval series, and monthly occurrences. A future domain due date
  is checked before invoking the model. After a turn, generic waits use the demo polling
  limit (30 seconds by default). The default of six automatic activations without new business information
  can block polling; this does not limit the task's total lifetime or a three-day due-date wait.

  Interval check-ins wait for completion before the next interval. Monthly schedules
  preserve calendar dates, use unique occurrences and skip missed windows. They are
  not unrestricted model-generated cron jobs. See `check_in_series.py`,
  `monthly_check_ins.py`, `provider_callbacks.py`.

  ## 7. Memory and knowledge

  The implementation has **authoritative current memory** and **searchable historical
  evidence**. They use different reads and cannot grant the same authority.

  | Concept | Actual representation |
  |---|---|
  | Firm knowledge | Approved `Instruction` rows with `FIRM` scope |
  | Provider knowledge | `PROVIDER` instructions tied to a contact; still tenant-scoped |
  | Firm-provider relationship | `FIRM_PROVIDER` instructions; not shared across firms |
  | Case memory | Case restrictions plus attributed sources, events and reports |
  | Task memory | Normalized `STOP_CONTACT`, `OBLIGATION` and optional `NOTE` items |
  | Episodic memory | Saved events, executions and sources describing past activity |
  | Semantic memory | Derived passages and optional embeddings for finding history |
  | Procedural memory | Agent instructions, approved policy and code-owned tool rules |
  | Working context | One activation's saved snapshot and its bounded model-facing copy |

  `memory.py` loads active instructions, holds and obligations through scoped SQL on
  every activation. A fingerprint and revision list support freshness checks before
  acting and committing. Holds and obligations need explicit resolution; a review date
  does not clear them. Informational notes can expire. Explicit supersession/resolution
  preserves links to the old source and reason (`memory_lifecycle.py`, `memory_api.py`).

  `memory_history.py` derives overlapping passages, exact/keyword/vector search and
  source-linked digests. Embeddings use `text-embedding-3-small` when enabled, with
  keyword fallback when unavailable. Digests are **extractive excerpts of the latest
  eight sources**, not a complete model-written case summary. Source hashes reject
  stale passages and digests. Historical recording dates are not treatment dates.

  Contradictions are not automatically reconciled into one true fact. Current controls
  remain in force; conflicting instructions can require review. Model observations
  include source IDs, but do not automatically become approved firm/provider knowledge.
  That promotion requires an operator-controlled instruction or memory change. There
  is no autonomous cross-case learning or shared cross-tenant knowledge base.

  ### Reported information from conversations

  `communications.py` provides shared CALL/EMAIL/MESSAGE transcript ingestion and a
  simulated-call form on active specialist tasks. It preserves the original Source,
  queues the existing outbox wake, and runs a tool-free LLM interpreter before context
  assembly. The model proposes categories and exact quotes; backend checks control
  task memory writes. Facts become reported notes, dated commitments become scheduled
  notes, restrictions become contact holds, and prerequisites become obligations.
  Broader scope, ambiguity, unsupported quotes and classifier failure require review.

  | Interpreter proposal | Backend write and effect |
  |---|---|
  | Task fact | Reported `NOTE`; does not verify delivery or grant permission |
  | Explicit dated task commitment | Dated `NOTE` retained in mandatory context; contact waits until due |
  | Contact restriction | `STOP_CONTACT`; human review required |
  | Prerequisite or proposed policy change | `OBLIGATION`; human review required; no policy is changed |
  | Unclear scope/date, unsupported quote or classifier failure | Review obligation; contact and completion pause |

  The interpreter has no action tools. It proposes classifications; application code
  decides which writes are allowed. Its version-1 analysis stores the prompt snapshot,
  prompt hash, model and trace reference separately from the unchanged raw transcript.
  A proposed new phone number stays reported information for review; routing is not edited.

  A single agent-attributed Activity comment is built from validated quotes and links
  to the original transcript. Retries do not duplicate memory or comments. Holds and
  obligations gate both contact tools and completion. Typed human review resolves only
  that communication's blockers; it cannot clear unrelated holds or publish firm policy.
  Medical authorization prerequisites require a task-scoped uploaded document and
  operator attestation. The next context includes a source-linked resolution so the
  old analysis is not mistaken for an open requirement.

  Explicit date-only commitments schedule follow-up at 09:00 in the submitted timezone.
  PostgreSQL stores the due note; Temporal waits durably. Medical reminder state also
  uses the date. Promises do not count as document receipt. The interpreter is bounded
  to 10,000 input characters, 12 claims, one model turn, 2,500 output tokens and 45 seconds.
  These checks do not prove semantic accuracy. Calls are simulated, speaker identity is
  unverified, and phone routing never changes automatically. See
  [conversation ingestion](communications.md) for the demo and limits.

  ## 8. Context engineering

  Workers receive focused task and domain state; the supervisor receives the case
  objective, plan, bounded task overview, case contacts and available capability cards.
  Supervisor memory reads case tasks independently of the bounded history list, so an
  old hold is not dropped merely because it is outside that list. Provider-specific
  supervisor instructions are selected from approved records targets; each worker's
  adapter supplies its own provider scope.

  An abbreviated worker context follows the actual keys in `execution.py`:

  ```json
  {
    "task": {"id": "…", "agent_key": "medical_records_collector", "agent_version": 3},
    "domain": {"adapter": {"key": "medical_records", "version": 1}, "state": {}},
    "memory": {"operational_items": [], "manifest": {"schema": "mandatory-memory.v1", "fingerprint": "…"}},
    "history": {"summary": null, "retrieval": {}},
    "recent_events": [],
    "new_events": [],
    "sources": [],
    "expected_version": 12,
    "agent_definition_hash": "…"
  }
  ```

  This is illustrative, not a complete API schema. Context also includes deadlines and
  other runtime fields. A new cycle reloads current state; a retry may reuse the saved
  snapshot and must still pass freshness checks.

  `context.py` first removes optional notes/history, then older events and long source
  bodies as needed. It retains source references and provides `retrieve_context` and
  `read_evidence` for bounded expansion. Older retrieval output can be shortened;
  mandatory controls and domain-action results are not silently discarded. Packing
  rechecks the wrapped model request, including JSON/message overhead, and removes more
  optional history until it fits. A worker whose required input still cannot fit fails
  with a `CONTEXT_BUDGET` execution incident and saved size diagnostics. It offers technical
  recovery rather than provider follow-up approval. No model decision is fabricated.
  See [the context-budget root cause and regression checks](context-budget-rca.md).

  This avoids sending the entire case history on every activation. The complete saved
  snapshot remains available for inspection; only model-facing copies are shortened.
  Vector matches are evidence candidates, never permission or completion proof.

  ## 9. Tools and security boundaries

  There is a shared checked-tool path, not a separate gateway service. `ToolSpec` declares
  name, description, typed arguments, effect (`read` or `contact`) and an optional logical
  action key. Contact tools require a task-scoped key. The registry checks that published
  agent tools and output schemas match their adapter (`domains.py`).

  For a call such as `send_records_reminder({"occurrence": 1})`, the runtime validates
  the arguments, allowed-tool list, call budget, task version, current memory and contact
  holds. The adapter checks due time, participants and remaining requirements. A source
  and `ToolInvocation` save the result and validated arguments. Subsequent calls with the
  same key reuse the result; different arguments for that key are rejected.

  The actual cache lookup in `execution.py` includes:

  ```python
  select(ToolInvocation).where(
      ToolInvocation.tenant_id == tenant_id,
      ToolInvocation.task_id == task_id,
      ToolInvocation.logical_key == key,
  )
  ```

  The model gets no unrestricted SQL tool and cannot choose tenant or parent-task scope
  through delegation inputs. Idempotency means a repeated logical action reuses its saved
  result. Sources and reports are treated as untrusted data in prompts;
  backend scope, policy and evidence checks provide the stronger boundary. This is not a
  proof that all prompt injection is prevented.

  Per-task call/reminder limits exist. There is no general tenant-wide rate limiter.
  Human decisions authorize specific transitions or continuations, not all future contact.
  No real email, SMS, fax, phone, provider portal or document-download integration is wired.

  ## 10. Events and delivery

  Business transactions save `ActivityEvent` rows and any needed inbox/outbox records
  together. A dispatcher polls pending outbox rows using `FOR UPDATE SKIP LOCKED`, starts
  or signals the stable Temporal workflow identity, then marks delivery. Failures retain
  pending work with backoff. Workflow-start retries reject duplicate identities.

  Incoming updates use idempotency keys and source identities. Inbox uniqueness prevents
  adding the same event to a task twice. Duplicate wakes without unread facts can reuse
  the previous outcome rather than spend another model call.

  Example: a provider update saves a `Source`, updates received requirements, emits
  `provider.response_received`, and adds a worker inbox entry and wake outbox message.
  The worker reloads evidence and later emits a completion/blocker event for the supervisor.
  Not every raw provider update directly invokes the supervisor.

  These are database-backed events, not Kafka or a general event bus. Consumers use saved
  inbox entries; timestamps and IDs help order reads, but there is no global total-order
  guarantee. Current rows, locks and revision checks resolve concurrent changes. This is
  not full event sourcing: business state is not rebuilt entirely by replaying events.

  ## 11. Audit journal

  A shared `audit.events` journal links the existing activity, execution, tool, source
  and human-decision records. Those tables still hold the full evidence. The journal
  stores safe references using one versioned event contract.

  | Contract field | Purpose |
  |---|---|
  | Event ID, schema version, occurrence/recording time | Identify the event and its format |
  | Firm, case, task and execution IDs | Locate the work |
  | Actor, action, resource and outcome | Record who tried what and what happened |
  | Reason code and policy version | Explain the backend authorization decision |
  | Request, trace/span and attempt IDs | Connect requests, traces and retries |
  | Evidence references and small operational details | Link full evidence without copying sensitive narratives |

  The journal records API and tool authorization decisions, tool attempts/results,
  approvals, execution outcomes, failed requests and external-effect attempts/results.
  Delegation proposals are labelled `PROPOSED`; cached calls are labelled `CACHED`.
  An allowed action that later fails stays distinct from a denied action.

  **Commit behavior:** deferred database triggers capture successful business writes
  in the same transaction, including ORM and raw SQL writes. Deferring capture keeps
  the audit-chain lock late in the transaction. Tool attempts and failure records use
  separate short transactions, so rolling back business work does not erase them.
  A crash can leave an attempt without a result; its outcome is unknown. Audit failure
  stops a tool before invocation; failure to audit a business commit rolls it back.
  Trace export remains best effort and is separate from this durable journal.

  **Permissions:** Compose API and worker use the non-superuser `agents_app` login.
  It can read the journal and execute the append function, but cannot insert directly,
  rewrite/delete rows, truncate the journal, disable its triggers or run retention
  maintenance. A separate `NOLOGIN` role owns the journal. Migration credentials stay
  separate from the running Compose services. This does not add per-user authorization.

  **Integrity and retention:** each firm stream has a sequence and SHA-256 hash chain.
  `scripts/audit_journal.py` verifies links and the stored head, exports a checkpoint,
  and can compare a previously saved checkpoint. The demo retention default is 365
  days, not a legal requirement. Only a separate maintenance role can prune an expired
  prefix using its archive hash; a legal hold blocks pruning. Policy changes cannot
  shorten existing saved retention deadlines. Pruning preserves the chain anchor.

  Database administrators can bypass these controls. Checkpoints must be stored
  independently to detect a full rewrite; signed exports and automatic off-host archival
  remain future work. Existing historical data is not backfilled with invented audit
  decisions. Downgrades detach business capture but preserve the journal; upgrades
  reattach it. Stop application processes during schema migrations.

  Read events through `GET /v1/firms/{tenant_id}/audit/events`, with case/task filters
  and sequence pagination. See [Audit design, research sources and commands](audit.md).

  ## 12. Observability and evaluation

  The API and worker use OpenTelemetry. Jaeger receives platform spans; self-hosted
  Langfuse receives the AI execution tree and backend evaluation scores. Both run locally.

  ```text
  Case → Task → Execution
                 ├─ Context assembly → Database operations
                 ├─ Agent run → LLM calls → Tool calls → Database operations
                 └─ Result commit
  ```

  Request and execution IDs, native Temporal workflow/activity headers, and trace
  context saved in the outbox connect the spans. Activities record retry attempts,
  queue delay, duration and errors. A workflow can wait without keeping a model or
  application span open; later activities join its workflow trace.

  Model spans capture redacted inputs/outputs, model name, pinned agent version,
  definition/prompt hashes, prompt version and per-call token usage. Langfuse estimates
  cost from its model-price registry. Tool spans capture latency, cache hits and errors.
  Database spans capture operation kind and duration, without SQL text or parameters.
  SDK lifecycle hooks provide model spans; the SDK's separate OpenAI trace export stays
  disabled to avoid duplicate or unredacted export. That setting does not disable this
  platform's OpenTelemetry or Langfuse integration.

  Free text, domain values, arbitrary payload keys and exception messages are removed
  before export. Both exporters apply a second filtering boundary. Original model
  inputs and stored business evidence are unchanged. This limits readable case detail
  in traces and does not replace protection of the application database.

  `Execution.context_json` saves trace references. The frontend's **Executions** tab
  links to the execution trace in Jaeger and the LLM trace in Langfuse.
  `telemetry.py` also emits correlated JSON logs. `operations.py` reports pending outbox
  age, runnable-work delay, failed tasks, overdue callbacks and human requests; its
  monitor logs alert-opened/cleared transitions without external notifications.

  Langfuse stores `backend_validation_passed` and `task_completed` scores. These check
  saved backend outcomes, not clinical correctness or general model quality. Scripted
  calls are labelled separately and have zero model cost. Live token usage comes from
  the provider; estimated cost is not a billing ledger.

  Langfuse storage uses persistent local volumes. Jaeger's demo store is in memory.
  Export uses bounded background queues and can lose traces during an outage; it does
  not roll back business work. Durable production trace storage, exported metrics,
  external alerts, model fallback and measured quality/latency targets remain unfinished.

  Tests cover evidence, scope, stale work, schedules, recovery, trace propagation,
  redaction, retries and audit integrity. Integration tests use disposable PostgreSQL
  databases and isolated Temporal queues. Opt-in live journeys verify mechanisms, not
  broad routing accuracy. Labelled planning, retrieval and escalation evaluations remain
  future work. See [Local tracing and verification commands](observability.md).

  ## 13. Human intervention

  A missing authorization, exhausted reminders, concerning client report or unclear
  objective can create a `HumanRequest` with context, evidence, allowed actions and a
  version. A response includes expected task/request versions and an idempotency key.
  The backend validates it and saves `HumanDecision`, evidence and wake records.

  Example: after automatic records reminders are exhausted, a person can authorize one
  specific follow-up with a follow-up date and response deadline. `HumanContinuation`
  stores that authorization. It does not clear contact holds or manufacture received records.
  Client-report acknowledgement applies only to that report; guidance alone does not approve it.

  Manual takeover is recorded through `ManualContact`, not represented as an agent send.
  A call note does not prove document receipt. Failed tasks still need explicit incident
  recovery. Overdue requests are visible operationally; there is no universal automatic
  approval expiry or unattended escalation service. New domains must supply any special
  human-response handling. See `human_requests.py`, `human_request_handlers.py`, `recovery.py`.

  ## 14. Failure handling and reliability

  | Failure | Current handling | Limit |
  |---|---|---|
  | Model unavailable or timed out | Record error; bounded Temporal retries; incident after exhaustion | No alternate provider or automatic failover |
  | Invalid output | SDK/Pydantic validation; evidence errors get bounded replanning | Schema-valid text can still be wrong |
  | Tool exception/timeout | Roll back its transaction; propagate or handle known domain rejection | No general external-tool timeout/reconciliation framework |
  | Simulated effect saved, final turn crashes | Tool result remains; retry reuses the logical key | Earlier successful tools are not undone |
  | External action succeeds, save fails | No real delivery path today; recovery forbids blind retry for `OUTCOME_UNKNOWN` | Future integration needs provider idempotency and reconciliation |
  | Duplicate invocation or wake | Saved outcome, tool key, inbox/outbox uniqueness and stable workflow identity | Execution/delivery can repeat; not exactly-once delivery |
  | Worker crash/activity retry | Temporal resumes control flow; saved context and effects are reused when valid | Worker availability controls when queued work runs |
  | Database unavailable | Transaction cannot commit; dispatcher/monitor report failure; activities retry | An extended outage can exhaust bounded retries |
  | Delayed event/stale reasoning | Reload current state; compare task versions and memory fingerprints | No global event order or arbitrary historical replay |
  | Human does not respond | Work stays blocked; overdue/age signal is visible | No general deadline-based reassignment |
  | Conflicting memory | Keep current controls; reject stale actions and request review where needed | No general automatic contradiction solver |

  Recovery preserves the business task and action keys but starts a new workflow identity
  and generation. Late work from the old generation is rejected. The demo limits explicit
  recoveries to three. The reliability model is **retryable delivery with idempotent
  handling**, with atomic simulated effects inside PostgreSQL—not exactly-once external action.

  ## 15. Security and multi-tenancy

  Scoped queries, foreign keys that include tenant/case IDs, and case-contact checks prevent many accidental
  cross-firm references. Retrieval filters by firm and case before using history. Agent
  configuration is global; case data and instructions are tenant-scoped.

  The operator bearer credential grants access to all firms. It is not end-user identity,
  role-based or per-firm authorization. No PostgreSQL row-level-security policies are configured.
  The restricted application database login protects audit history; it does not add
  tenant-specific database access rules. A developer plugin is trusted Python imported
  from the installed package, not a sandbox.

  Secrets come from environment settings; the operator key uses `SecretStr` and constant-time
  comparison. Compose has development defaults. Application lifecycle logs avoid model/provider
  content, and trace exporters redact payloads. Source/context tables still contain sensitive
  text; there is no complete field-encryption or application-data redaction layer. Model and embedding calls can send selected content externally.

  Protected health information (PHI) needs more protection before real use. Production needs
  firm-bound identity, access review, encrypted connections, secret rotation, storage protection,
  retention/deletion and an appropriate PHI/security review. Do not place real PHI in this demo.
  Health/docs and development probe routes also need deliberate production exposure controls.
  No HIPAA compliance or production security certification is claimed.

  ## 16. End-to-end examples

  **Medical records:** use the fictional firm/case and contacts created by `seed.py` or
  `examples/create_walkthrough.py`; case creation is setup, not an agent capability.

  1. The operator submits an objective with approved hospital targets, or approves a
     natural-language assignment plan. The planner has a separate coverage-review pass.
  2. The transaction saves the coordinator, objective event, inbox and wake outbox record.
  3. The dispatcher wakes `CaseWorkflow`. The supervisor loads current case context and
     proposes work. Backend checks reuse, overlap, roles and approved scope.
  4. A child task pins the records agent version. Its records request, typed requirements,
     creation event and start outbox record are saved together.
  5. `TaskWorkflow` invokes the runtime. The worker loads instructions, holds, outstanding
     requirements and evidence, then calls the checked provider-detail/request tools.
  6. The simulated send creates saved tool evidence. A validated wait outcome gives
     Temporal a date to wait for. Follow-ups ask only for still-missing items.
  7. A simulated provider response saves a source and links received items to requirements.
     Partial receipt leaves the other items outstanding; a callback can defer further contact.
  8. The inbox/outbox wakes the same worker. It reloads current facts and checks evidence
     coverage. Uploaded text-based PDFs can be parsed; there is no OCR or clinical validation.
  9. Completion or blocking is committed with source-linked observations and an event.
     That event wakes the supervisor. The supervisor verifies approved coverage or requests
     human review; it does not close the legal case.

  For the historical trail, follow `task.created` → the initial `ToolInvocation` and
  `Source` → `provider.response_received` → `RecordEvidence` → worker execution outcome
  and `task.completed` → `supervisor.review_completed`. The common audit journal adds
  authorization decisions, attempts, approvals and committed-effect records. Join through
  firm/case/task, execution and source IDs; use trace references for timing and model/tool
  calls. Provider contact remains simulated.

  **Partial hospital response and authorization:** `provider-responses` saves the original
  message and attachment bytes, then wakes the existing worker. The adapter's ingestion
  hook parses bounded PDF/text files outside database locks. A structured LLM extraction
  cites actual attachment pages and provider statements; backend code checks those citations.
  General records and surgical notes are separate requirements. Forty pages of general
  records cannot complete the missing surgical-notes requirement.

  The extracted authorization request becomes a source-linked task-memory obligation and
  typed human request. Both contact tools and completion are blocked. The teammate must
  supply a task-scoped authorization document, attest to its review and choose a deadline.
  The saved decision authorizes one simulated follow-up for missing items. Approval does
  not count as document receipt. The same task resumes after a worker restart; it can
  complete only after surgical notes arrive and evidence checks pass.

  Extraction version 2 stores its prompt snapshot/hash with each analysis and separates
  outstanding requirements from prior coverage. Records-agent version 4 requires human escalation for missing authorization.
  Supervisor version 4 clarifies event/source citation types and links specialist review
  questions to child tasks. State-derived evidence reviews clear after coverage verifies;
  human authorization reviews still require a typed decision. Earlier definitions
  remain stored. Model proposals are recorded before backend changes, so behavior checks
  can distinguish a correct agent choice from a backend safety correction.
  [The repeatable hospital scenario](hospital-response.md) includes real Temporal execution,
  optional live model calls, generated fictional PDFs, trace links and a machine-checked
  report. Scanned/ambiguous files require review; identity, signatures and real delivery
  remain outside this slice.

  **Information from a simulated call:** open an active specialist task and expand
  **Simulate call**. For example: “We found the records. Do not call about this request
  until you receive an updated authorization. Please provide an updated authorization.”

  1. `POST /v1/firms/{tenant_id}/tasks/{task_id}/demo-call` validates the operator and
     case contact. Shared `/communications` intake also accepts EMAIL and MESSAGE text.
  2. One transaction saves the original transcript Source, Activity event, inbox item
     and outbox wake. The dispatcher signals the existing task workflow.
  3. The worker classifies the transcript outside database locks. Backend checks exact
     quotes, dates and scope, then saves the analysis, reported memory and one agent
     Activity comment linking to the transcript. Replays do not duplicate those writes.
  4. The restriction and authorization prerequisite block contact and completion.
     The teammate reviews the source, uploads a task-scoped authorization and records
     an explicit decision. Only this communication's blockers are resolved.
  5. The decision queues another wake. The same task resumes with fresh memory and
     a source-linked resolution record, including after a worker restart. The old
     transcript and interpretation remain available. Approval is not document receipt.

  `examples/communication_demo.py --live` exercises this flow with OpenAI and real
  Temporal in a disposable database. A second transcript promises records on an
  explicit future date; it creates a dated commitment and a wait, leaving records
  outstanding. Without `--live`, classification uses a labelled regex baseline.
  No real call, email connection or audio processing is included.

  **Client check-in:** approval creates `ClientCheckIn` with `scheduled_at`. Before that
  date, the runtime returns a wait without invoking the model. At the date, checked tools
  simulate a send and wait for a reply. A structured self-report is compared with the
  previous report; configured concerns require review. This reuses task execution, timers,
  context, evidence and audit infrastructure. Free-text clinical understanding is not established.

  ## 17. Adding Insurance Follow-up

  A developer implements a typed assignment contract, domain tools, context loading,
  completion checks and any special human-response handling. Register them in an
  `agent_plugins/` module exporting `CAPABILITIES`. `AgentCapability.expose_to_supervisor`
  defaults to `True`; the definition supplies instructions, model, tool names, output
  schema and execution limits.

  At worker startup, discovery publishes definitions without replacing existing versions.
  Each supervisor activation reads the available published catalog. `delegation.py`
  generates a typed `delegate_<agent_key>` tool; its call queues a proposal. Backend code
  validates the saved capability/version, scope and input before creating a durable child.
  The assignment planner and dashboard use the same catalog. No insurance-specific branch
  is needed in the core supervisor. Module changes require normal deployment/restart.

  | New code | Configuration | Reused |
  |---|---|---|
  | Insurance input/output contracts, checked claim tools, adapter validation and evidence rules | Prompt, model, budgets, capability description and exposure flag | Runtime, task/exec rows, timers, delivery, generic memory/retrieval, logs and audit records |
  | Optional factory for claim-specific business tables; callbacks and human handlers | Published agent version | Default JSON-input task factory when extra business rows are unnecessary |

  The example is proposed work, not a shipped insurance agent. The catalog currently
  selects one registered version per agent key; older task definitions remain stored,
  but compatible executable code must remain available. See `docs/adding-agents.md`.

  ## 18. Implementation status

  | Capability | Status | Evidence | Remaining work |
  |---|---|---|---|
  | Agent runtime | Implemented | `execution.py`; runtime/domain tests | Provider failover and stronger spend controls |
  | Durable orchestration | Implemented | `orchestration/workflows.py`; restart tests | Production Temporal deployment and capacity tests |
  | Persistent memory | Implemented, bounded | `memory.py`, `memory_history.py`, lifecycle tests | Knowledge curation and retrieval-quality evaluation |
  | Conversation ingestion | Working text slice | `communications.py`, simulated-call UI, focused tests and live Temporal demo | Real channel adapters, identity checks and classification-quality evaluation |
  | Context assembly | Implemented | `context.py`; budget/retrieval tests | Explicit packet migrations and large-case tests |
  | Supervisor/discovery | Implemented, bounded | `coordination.py`, `capabilities.py`; third-agent tests | Broad planning evals and adapter upgrade strategy |
  | Tool checks | Implemented for simulated tools | `domains.py`, `execution.py`, adapters | Real delivery, provider rate limits, reconciliation |
  | Audit | Working slice | Common journal, authorization/failure events, restricted writer, retention/holds and hash-chain verification | Independent signed checkpoints, archival automation and production identity |
  | Observability | Implemented tracing; partial operations | `observability.py`, `telemetry.py`, `operations.py`; trace/retry tests and local demo | Durable production trace storage, exported metrics and external alerts |
  | Human intervention | Implemented for shipped domains | Human requests/handlers and recovery tests | New-domain handlers and broader escalation policies |
  | Tenant isolation | Partial | Scoped SQL, constraints and negative tests | User identity, database row-level access rules and security review |
  | External outreach/document intake | Simulated | Domain tools, response APIs, demo scripts | Actual provider/client integrations and document processing |
  | Agent quality evaluation | Bounded scenario evidence | Hospital and conversation scenarios check sourced extraction, blockers, restart and typed review; hospital checks also distinguish model proposals from backend safeguards | Broader labelled datasets, repeated trials and release gates |

  Tests demonstrate mechanisms, not measured production scale or correctness on all inputs.
  Observability was checked with scripted and live-model journeys in disposable databases.
  Audit checks cover rollback, denied actions, restricted-login execution, concurrent appends,
  tampering, retention, legal holds and migration compatibility. The hospital scenario has
  13 explicit behavior checks, including the model's own human-escalation choice before
  backend validation. One live run passed all 13, with actual Temporal execution and worker
  restart. Negative tests cover unsupported receipts, blocked contact and early completion.
  The conversation scenario passed 11 checks with actual OpenAI classification and
  Temporal, including source preservation, one agent comment, protective memory,
  review, restart/resume and a dated promise that does not count as receipt. Seven
  focused conversation tests cover grounding, dates, replay conflicts, classifier
  failures, typed resolution, unrelated holds and operator correction of reported notes.
  Frontend checks cover the call form and required review/document fields. See
  [the conversation guide](communications.md) for commands and limits.
  These are bounded examples, not broad model-quality or production-scale guarantees.

  Invalid assignment-planner output gets one correction attempt, within a shared
  60-second deadline. A repeated contract failure becomes a saved non-actionable
  clarification, while infrastructure errors remain HTTP failures. A retry records
  that earlier failed-attempt token usage is not included in the saved counters;
  model traces remain the source for those calls where available.

  Explicit email/web destinations must be represented in a registered agent's
  validated operational input. Built-in collection/check-in steps cannot silently
  drop delivery while calling the plan ready. Summary prose is not a delivery action.
  This literal-destination check complements model coverage; it is not a complete
  parser for arbitrary requests or permission to bypass domain/channel checks.

  Assignment intake also binds selected providers to full listed names in the
  original request, or explicit all-provider scope, before saving a ready plan and
  again before approval. A valid case contact UUID alone does not prove the user
  meant that provider. Unrecognized aliases require clarification; the model
  coverage reviewer still checks omissions, negation and remaining requested work.

  The browser rehearsal also completed both domains with real model calls and Temporal:
  partial documents, authorization approval while the worker was stopped, one resumed
  follow-up, final surgical notes, and a timed client concern followed by human review.
  Specific source reviews supersede generic follow-up forms, including during startup
  backfill. The UI keeps active reviews first, folds previous reviews and incidents,
  shows queued intake and selected filenames, and exposes check-in actions in the header.
  See [the rehearsal report](demo-rehearsal.md) for evidence and limits.

  ## 19. Architectural choices and tradeoffs

  The alternatives below explain the current design; they are not a claim that all were
  implemented or experimentally compared.

  | Problem and current choice | Alternative | Benefit and cost | Revisit when |
  |---|---|---|---|
  | SDK loop plus custom prepare/commit | Fully custom model loop | Reuse tool/output handling; still maintain state boundaries | SDK constraints prevent needed execution control |
  | Temporal for long waits | Database scheduler/poller | Durable waits/retries; adds a service | Operational cost outweighs workflow complexity |
  | SQL current state, derived vector history | Vector store as main memory | Holds/evidence stay deterministic; retrieval still needs tuning | Search volume needs a separate index, preserving SQL authority |
  | Short activations with saved context | One persistent agent process | Survives restarts and bounds context; rebuilds context each turn | A use case truly needs a live low-latency session |
  | Case supervisor with durable children | Flat independent workers/direct calls | Case coordination and reuse; more state and bounded planning | Dependencies require a richer planning model |
  | Business tables plus a common audit journal | Only workflow history/full event sourcing | Shared event contract without replacing current state; evidence references still need joins | Replay requirements justify fuller event modelling |
  | Per-firm audit hash chain and restricted writer | Independent audit service with signed storage | Small local change; serializes appends and needs independent checkpoints against administrator rewrites | Volume or stronger trust separation requires it |
  | Modular monolith and DB outbox | Microservices and broker | One transaction boundary and small stack; shared deployment/DB load | Independently scaled workloads justify new service boundaries |

  ## 20. Production priorities

  **Before real firm data:** replace the shared operator credential; restrict deployment
  endpoints; review PHI flow to model/embedding providers; add encrypted connections, managed secrets,
  field/storage encryption, reviewed retention policies and tested backup/restore procedures.
  Keep migration credentials outside runtime services, grant maintenance access separately,
  and store verified audit checkpoints independently. Add signed archival and restore drills.

  **Before real document intake:** isolate PDF parsing with resource limits, add object
  storage and OCR where required, and define patient/scope/signature verification.

  **Before real outreach:** implement provider-side idempotency, outcome reconciliation,
  callback authentication and delivery evidence. Add tenant/provider rate limits and
  bounded retries. A database transaction cannot roll back an email already sent.

  **Before higher volume:** run concurrency/load tests, define queue limits and
  per-tenant budgets, size workers and database pools, index measured bottlenecks, and
  operate Temporal beyond its development server. `SKIP LOCKED` permits parallel delivery,
  but the code is not a scale benchmark or complete fairness mechanism.

  **For reliable releases:** add labelled planning/retrieval/escalation evals, compatible
  agent/adapter rollout rules, production trace retention, metric export, external alert routing and runbooks.
  Later consider stronger worker isolation, model fallback, dedicated search infrastructure
  and richer task dependencies only when measured needs justify them.

  ## 21. Final architecture summary

  - **Five decisions:** separate assignments from turns; separate business state from
    timers; use inbox/outbox delivery; check model actions in code; keep current controls
    separate from retrieved history.
  - **Five implemented strengths:** durable waiting/resume; saved versioned definitions;
    repeat-safe simulated tools; source-based completion checks; registry-driven delegation
    with human review/recovery for the shipped workflows.
  - **Three main limits:** simulated external effects; incomplete identity/audit/security
    hardening, including independent audit archival; no broad measured agent-quality or production-scale guarantee.
  - **Three first improvements:** firm-bound identity and data controls; real delivery
    with reconciliation; labelled evals with operational release gates.

  The reusable unit is the typed domain capability. A new domain supplies its own inputs,
  actions and evidence rules while reusing the same persistence, execution and waiting
  mechanisms. Reuse does not remove the need to implement that domain's business checks.


## Local demo update: Luna and readable traces

New assignments use `gpt-6-luna` (records v5, supervisor v5, check-in v2).
Published older definitions remain unchanged. Reasoning is set to `none` for these
bounded turns. Planner, coverage, call interpretation and document extraction use
`OPENAI_MODEL`. The current Interview live assignment was explicitly upgraded;
completed tasks and saved executions retain their previous versions.

Local Langfuse can record readable model/tool payloads with
`LANGFUSE_CONTENT_MODE=local_debug`. Credentials remain masked and Jaeger stays
redacted. Default deployments retain redaction; readable mode requires local
Langfuse and the local environment. Old redacted exports cannot be recovered.

The operator task list includes parent assignments and specialist children. Each
parent has a Subtasks column linking directly to its children. Parent list status
reflects coordinator state, with filtering and pagination applied consistently.
Parent activity contains only parent events; original specialist updates stay on
child tasks. The API retains the agent-only list by default for existing clients;
the UI opts into parent rows with `include_parents=true`.


### Retry-friendly demo setup

New records assignments default `reminder_interval_seconds` to 3600 seconds in the
creation contract and save it on the records request. The prior 30-second demo
cadence exhausted three reminders before an operator had time to explore the UI.
The reminder limit still applies; this change does not remove review safeguards.

Interview live was reset at the user's request: old workflows were terminated,
run data removed after a local backup, and the case received a fresh internal ID.
Contacts and instructions were retained. A fresh case ID avoids reusing an old
terminated supervisor workflow ID; the audit journal and other workspaces remain.


### Clear first-request instructions and operator wording

Seed instructions explicitly allow an approved task's first records request. Only
later follow-ups require reusing its saved reference. A platform-generated reference
is not a prerequisite for creating a new specialist. Existing evidence of an older
request still requires reuse or review. Operator-facing model summaries use short,
plain sentences and ask one clear question. Clarification actions say “Update the
task details”. Published instruction revisions retain the previous wording.

An expired supervisor attempt now returns `CONTINUE` with its execution marked
`STALE`, preparing a fresh bounded turn instead of retrying an expired deadline.


### Routine callback timing

A plain “call back in three days” is scheduling, not authorization. A bounded,
code-owned recognizer handles a sole relative callback directive (1–30 days),
optionally preceded by a simple records-not-ready statement. Provider-response
intake saves the callback on the existing records request, anchored to original
receipt time. Simulated calls save a source-linked commitment and defer the same
request. Neither path counts documents as received or grants contact permission.
Additional conditions, conflicting dates or sensitive language remain in the normal
interpretation/review path. Current holds still apply. The recognizer is a small
supported-language slice, not a general natural-language date parser.

Resolved provider reviews now have a current-state projection; historical
`needs_review` is not an outstanding blocker. A routine timing update must not
reopen that approval solely because the original extraction requested review.


The routine relative-callback recognizer also accepts a simple document promise
followed by the same-time callback, such as “will send you the docs in 5 days,
call us that time”. It saves a sourced expectation and wait, not receipt evidence.
Additional conditions remain outside this timing-only fast path. The call form
now explains that clear relative delays are supported; full calendar dates still
need a year.


### Fictional demo assumptions

The local demo enables `DEMO_ASSUME_ROUTINE_UPDATES=true`; the default elsewhere
is false. Grounded facts and commitments with unknown scope may be treated as
current-task reports. A clear relative delay uses the original call time. Vague
timing uses a three-day follow-up and records that assumption in the saved claim
and Activity summary. Conflicting delays, unsupported quotes, explicit broad scope,
restrictions, authorization, consent and routing changes still require review.
Assumptions never prove document receipt or clear existing holds.


### Demo-wide behavior policy

`demo_policy.py` supplies the same optional guidance to assignment planning, coverage
review, the supervisor, specialists, communication classification and document
extraction. Local demo mode favors stated defaults, short updates and waiting over
nonessential clarification. The actual effective prompts are visible in model traces;
intake/extraction snapshots record the demo setting or effective prompt. Existing
evidence, scope and permission gates still validate proposed actions. The policy
changes reasoning guidance, not the facts needed to claim success.


### Offline tokenizer and technical recovery

The app image preloads `o200k_base` and `cl100k_base` into
`/opt/tiktoken-cache` during the build. Token counting and clipping are verified
with Docker networking disabled, so an encoding download cannot interrupt an agent
turn. Previously a cold container tried to download the encoding at runtime and
raised `ConnectionError` before the model request.

Technical retries preserve open business reviews. If recovery must recreate a
previously superseded review for a still-open subject, its deduplication key includes
the recovery generation. A service fix does not grant authorization or consent.


Human-authorized records continuations also honor any later callback or sourced
communication commitment. Approval supplies permission for the follow-up; it does
not override its timing. Early verified receipt still lets the same task finish
without waiting for that future contact.


Assignment coverage review now receives a normalized business summary rather than
the full agent catalog plus differing intake schemas. Check-in summaries include
the resolved message, client, time and one-off cadence. Pydantic/domain code owns
technical field validation; the reviewer checks business omissions. This fixes a
false clarification claiming `message` was absent even though it was already saved.

### Built-in workspace reporting in assignment coverage

The intake coverage reviewer previously saw only a collection step and could treat
“tell me about missing items or anything that needs my help” as uncovered extra work.
The coverage packet now explicitly includes the runtime's automatic progress,
missing-item and blocker reporting through task Activity and the human review queue.
Records work summaries also include that reporting. Planner v4 / coverage reviewer v5
(router v6) count this behavior without adding a reporting assignment or asking for
confirmation. Surgical notes are explicitly listed among supported record types.
External delivery remains separate and unsupported unless a registered capability
actually provides it. Three live runs of the reported typo-containing request and
the exact walkthrough prompt returned ready plans; external PDF email delivery
still required clarification. The affected intake regression suite passed 26 tests.

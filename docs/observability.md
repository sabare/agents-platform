# Local tracing: OpenTelemetry and Langfuse

The API and worker use one OpenTelemetry pipeline. Jaeger receives platform spans;
Langfuse receives the AI execution tree and evaluation scores. Both run locally.

## Start

From the repository root, with the virtual environment active and Docker running:

```sh
python scripts/setup_observability.py
docker compose --env-file .env.observability -f compose.observability.yaml up -d
docker compose up -d --build api worker
```

Or use `make observability-up`, then rebuild the API and worker. Setup generates local
credentials in `.env.observability`, copies the client settings into `.env`, and never
prints secrets. Both files are ignored by Git. Re-running setup preserves the local
credentials. Langfuse uses separate PostgreSQL, ClickHouse, Redis and object-storage
volumes; it does not use the application's database.

- **Langfuse:** <http://localhost:3000>. Login email: `admin@hellocounsel.local`.
  Read `OBS_USER_PASSWORD` in `.env.observability` for the password.
- **Jaeger:** <http://localhost:16686>.
- **Application:** <http://localhost:8000>.

Only dashboard and OTLP HTTP ports are published, bound to localhost. Langfuse storage
persists across container restarts. Jaeger's local all-in-one store is in memory;
restarting it loses its stored traces. This is a development deployment.

## What one execution shows

```text
HTTP assignment
  outbox.dispatch → Temporal start / activity
    case                         case.id / tenant.id
      task                       task.id
        agent.execution          execution.id / retry attempt
          context.assemble
            db.operation
          agent.run
            llm.call             model, prompt version/hash, tokens, cost
            tool.<name>          duration, redacted input/output, cached result
              db.operation
            llm.call
          result.commit          saved outcome and database operations
```

The supervisor has the same structure. Its child-task start keeps the trace context
through the transactional outbox. `TracingInterceptor` carries it through Temporal
headers. The workflow waits without keeping a model or application span open; later
activities join the workflow trace. Different HTTP updates may also have their own
request traces. Workflow/activity IDs connect the records even across restarts.

The frontend's **Executions** tab offers **Open execution trace** and **Open LLM trace**.
The API also returns `observability` with trace ID, execution span ID and dashboard URLs.
These references are saved in `Execution.context_json`; existing execution rows without
a trace remain readable.

## What is captured

| Area | Capture |
|---|---|
| API and activities | Request/operation spans, status, duration and correlated IDs |
| Temporal | Native client/workflow/activity propagation, run ID, activity ID/type, retry attempt and queue delay |
| Context | Separate assembly span, counts and redacted structure |
| Models | A generation span for each SDK LLM turn, including failures; embedding spans when used |
| Model versions | Model name, pinned agent version, definition hash and system-prompt hash |
| Usage and cost | Per-call token usage; Langfuse calculates estimated cost from its model-price registry |
| Tools | Timed callbacks, redacted arguments/results, cache hits and failure status |
| Database | SQLAlchemy query duration and operation kind; no SQL text or parameters |
| Result | Commit span plus terminal/waiting state |
| Evaluations | `backend_validation_passed` and `task_completed` boolean scores, attached to the execution observation |

Scripted generations are labelled `runtime.mode=scripted` and have zero model cost.
Real generations use provider-reported usage. Langfuse cost is an estimate, not an invoice.
Backend scores test saved validation/completion outcomes; they do not score clinical
correctness or general model quality. Stale/replan-only outcomes are not given these scores.

The application uses supported SDK lifecycle hooks around `Runner.run`. The SDK's
separate OpenAI tracing export remains disabled to avoid duplicate or unredacted export.
OpenTelemetry and Langfuse still receive the custom generation and tool spans.
SQL uses public SQLAlchemy events because the installed SQLAlchemy 2.1 version is outside
the auto-instrumentor's supported range. No dependency check is bypassed.

## Redaction and failure behavior

Payloads are copied and redacted before being attached to spans. All free text, arbitrary
payload keys and domain values are removed; known structural fields, tool names and
control states can remain. This deliberately favors privacy over readable case narratives.
Original application data and the model's actual inputs are not changed.

A second exporter boundary protects **both** destinations. It keeps approved operational
attributes and removes SQL statements, parameters, URL query strings, exception text,
stack traces and arbitrary attributes. Application UUIDs and workflow identities remain
for correlation. This is an explicit data-minimization rule, not a claim of PHI certification.

Background database polling does not flood Langfuse: it receives spans marked as part
of an AI execution. Jaeger receives the configured platform traces. Export runs through
bounded background queues. Export failure is logged by error type and does not roll back
business work; traces can be lost during an exporter outage. Telemetry is not the audit ledger.

`OTEL_ENABLED=false` disables the pipeline. Host processes use localhost endpoints;
Compose uses `host.docker.internal` to reach the separate local observability stack.
Set `SERVICE_VERSION` to a release or commit identifier when comparing deployments.

## Verify a complete journey

```sh
python examples/observability_demo.py
# Optional: real model calls, which incur OpenAI usage
python examples/observability_demo.py --live
```

The demo creates a disposable database and isolated Temporal queue. It delegates an
approved records assignment, waits, simulates a provider receipt and verifies completion.
It exports traces, then removes the temporary database; it adds no firms to the running
application workspace. Links are printed and saved in `work/observability/latest-demo.json`.
The scripted path checks mechanics; the live path checks real model usage and cost.

`tests/test_observability.py` checks trace parentage, model/tool turns, database spans,
redaction, export failure, parallel identity isolation, evaluation scoring and Temporal retry
correlation. Existing task, context and supervisor tests cover business behavior.

Langfuse v4 reads use `GET /api/public/v2/observations?traceId=...`, with cursor pagination.
The old `/api/public/traces` read API is not available in v4's events-only mode. Jaeger v2
uses `/api/v3/traces/<trace_id>`. Application links open the dashboards, not these APIs.

To stop only observability services without deleting their volumes:

```sh
make observability-down
```

## Source map

- `src/agents_platform/observability.py`: provider, safe exporters, model hooks, SQL spans and score export.
- `telemetry.py` in that package: activity hierarchy and log/trace IDs.
- `orchestration/client.py`: Temporal tracing interceptor.
- `dispatch.py`, `tasks.py`: durable outbox trace propagation.
- `execution.py`, `coordination.py`, `context.py`, `delegation.py`: traced execution phases and tools.
- `compose.observability.yaml`, `scripts/setup_observability.py`: local services and initialization.

Primary references: [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/),
[Temporal Python tracing](https://github.com/temporalio/sdk-python#opentelemetry-tracing),
[Langfuse self-hosting](https://langfuse.com/self-hosting/deployment/docker-compose),
[Langfuse masking](https://langfuse.com/docs/observability/features/masking), and
[Langfuse public API](https://langfuse.com/docs/api-and-data-platform/features/public-api).

## Readable local model traces

`LANGFUSE_CONTENT_MODE=local_debug` records readable prompts, context, model outputs
and tool payloads in local Langfuse. This mode requires `OTEL_ENVIRONMENT=local`
and a local Langfuse hostname. Credentials remain masked; Jaeger payloads remain
redacted. The default is `redacted`. The local demo `.env` opts into readable mode.
Select an `llm.call` generation in Langfuse and open **Preview → Input / Output**.
Older redacted exports cannot be restored; inspect a new call. Historical prompts
and replies already saved by the application remain available in task executions.

New assignments use GPT-6 Luna through medical records v5, supervisor v5 and client
check-in v2. Older definitions are retained unchanged. The current Interview live
assignment was explicitly upgraded; its history, blockers and prior snapshots remain.
The task list shows parent assignments and a Subtasks column linking to original
child tasks. Parent activity contains only parent events; specialist activity stays
on each child. No duplicate comments or model calls are created for navigation.

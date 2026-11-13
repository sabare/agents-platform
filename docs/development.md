# Development and demos

## Local setup

Use Docker Compose for PostgreSQL, Temporal, migrations, API and worker:

```sh
cp -n .env.example .env
docker compose up --build -d
docker compose run --rm migrate python -m agents_platform.seed
```

Seeding creates fictional firm/case/contacts and published definitions. It is explicit and repeatable; published definitions are not overwritten. Tasks stay pinned to their assigned version.

| Surface | Address |
|---|---|
| Dashboard | http://localhost:8000 |
| API docs | http://localhost:8000/docs |
| Liveness / readiness | http://localhost:8000/health/live · http://localhost:8000/health/ready |
| Temporal UI | http://localhost:8233 |
| PostgreSQL | localhost:5432, database/user/password `agents` |

The dashboard is served by FastAPI; no frontend build/server is required. The API docs remain available during infrastructure outages; readiness checks PostgreSQL and Temporal. The development operator key is entered through Swagger **Authorize** or dashboard connection settings. `OPERATOR_API_KEY` defaults to `local-development-only`; the OpenAI key stays on the server. This is not per-firm user authentication.

PostgreSQL and the Temporal development server use persistent volumes. `docker compose down` preserves them; `docker compose down -v` deletes them. Temporal's local server stores its state in SQLite and is not a production deployment.

For host development and demo scripts:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

To run API/worker on the host, start only infrastructure and then use separate activated terminals:

```sh
docker compose up -d postgres temporal
make migrate seed
make api
# In another activated terminal:
make worker
```

Choose host or Compose API/worker processes for development. Both use the same configured database, namespace and task queue unless changed.

## Modes and configuration

`.env.example` lists configuration. `AGENT_MODE=scripted` uses deterministic SDK model responses for offline demos/tests. `AGENT_MODE=openai` requires `OPENAI_API_KEY`. Published durable definitions select their model; `OPENAI_MODEL` configures the original CLI scaffold. Real model calls do not make provider/client integrations real.

`DEMO_FOLLOW_UP_SECONDS` defaults to 30 and caps specialist follow-up delays. `DEMO_MAX_ACTIVATIONS` defaults to six and bounds automatic follow-up cycles before review. Context input/output/call limits are described in [memory and retrieval](memory-and-retrieval.md).

After changing code or Compose environment, rebuild/recreate the affected services:

```sh
docker compose up --build -d api worker
docker compose logs -f api worker
```

## Walk through the product

Use the dashboard to create a records task or client check-in. Open a task for comments, event history, executions, tool results, usage and starting context. A plain comment is recorded; use **Wake assigned agent** for an explicit specialist wake. Supervisor mentions wake configured case coordination.

The memory/context area supports obligations, task/case/provider holds, explicit resolution, history search and source inspection. Provider updates and client replies are entered through simulation controls. Instruction revisions and focused retrieval are also available through the API; not every API has a dedicated dashboard editor.

In **Supervisor**, submit an objective with approved records targets. The current UI supports one provider with all dates; the API supports up to ten explicit targets. Editing preserves existing multi-provider/date scope. Client check-ins are manually assigned and monitored; autonomous check-in delegation is not implemented.

## Demo scripts

Run from the repo in the activated host environment while the stack is up:

| Command | Demonstrates |
|---|---|
| `python examples/demo.py --restart-worker` | Records request, durable wait, worker restart, simulated receipt and completion |
| `python examples/supervisor_demo.py --restart-worker` | Objective replay, duplicate-safe delegation, specialist completion and objective verification |
| `python examples/memory_demo.py` | Old evidence retrieval, case hold/release, obligations and worker restart |
| `python examples/check_in_demo.py` | Scheduled sends, baseline/changed client reports, restart recovery and teammate review |

Scripts create fictional data. Restart scenarios stop/restart the Compose worker and temporarily affect other local demo work. They never send real communications. The legacy `agents-platform "assignment"` CLI calls specialists inline; it is not the durable API/worker path. `agents-platform --check` only checks its wiring.

## Main API entry points

All business routes use the development operator credential. Prefix these with `/v1/firms/{firm}` and use [Swagger](http://localhost:8000/docs) for exact fields and generated schemas.

| Route | Purpose |
|---|---|
| `POST /tasks/medical-records` | Create a records assignment; example body in `examples/medical-records-task.json` |
| `POST /tasks/{task}/provider-updates` | Simulate `PENDING` or `RECEIVED`; received documents are synthetic |
| `POST /tasks/client-check-in` | Schedule one check-in for a case/client with a timezone-aware send time |
| `POST /tasks/{task}/client-replies` | Record one attributed reply after the simulated send |
| `GET /cases/{case}/client-reports` | Latest 100 reports; optional client filter |
| `GET/POST /cases/{case}/supervisor` | Read coordinator state or submit/replace a records objective |
| `GET /tasks/{task}` | Read current state and domain projection |
| `GET /tasks/{task}/events` | Read timeline |
| `GET /tasks/{task}/executions` | Read saved context, tools, decisions, outcome and usage |

Creation/intake endpoints require `Idempotency-Key`: same key/body reuses the result; conflicting reuse returns `409`. Replacing a supervisor objective requires its `expected_revision`. Terminal records tasks reject late updates. A check-in accepts one reply; another check-in requires another task. Memory/retrieval routes are listed in the [memory guide](memory-and-retrieval.md).

## Checks and evidence

```sh
make check   # Ruff lint/format and CLI wiring
make test    # Tests without external infrastructure
make smoke   # PostgreSQL + Temporal integration tests
```

Integration tests create disposable databases and isolated workflow queues. PostgreSQL test credentials need create/drop-database permission; `TEST_POSTGRES_ADMIN_URL` can override the default local connection. Tests do not clear the application database. Use `pytest --collect-only -q` for the current inventory rather than treating a count in prose as a guarantee.

Coverage includes scope/relationship checks, duplicate intake, transactional rollback, migrations, restart/timer recovery, stale decisions, completion evidence, contact holds, client-report comparisons and SDK tool invocation. Focused retrieval tests include 240 distracting updates across seven months, provider/date selection, exact/vector scope checks, evidence pagination and context overflow.

Scripted tests establish execution behavior, not real-model answer quality. Synthetic vectors establish SQL ranking/isolation, not embedding relevance. Past real-model demos are useful smoke checks, not a production benchmark or proof of months of continuous operation. This documentation cleanup does not rerun those demos.

## Add another specialist

`domains.py` defines the code-owned adapter contract. `medical_records.py` and `client_check_in.py` are the two shipped adapters.

1. Define typed assignment/result schemas and saved domain state.
2. Implement `load`, tool declarations, `execute_tool`, `validate`, `review` and a scripted test model. `load` supplies domain state and relevant provider IDs; `validate` supplies evidence checks and a next action.
3. Register the task-type/input-schema/agent-key binding. Unknown bindings, unsupported tools/results and incompatible saved adapter versions fail closed.
4. Publish a new immutable agent definition, add intake/update APIs and UI where needed, and test the workflow end to end. Supervisor delegation needs explicit support; registering an adapter does not add automatic planning for it.

The shared executor owns identity, snapshots, versions, memory, domain-tool allowlists, tool-result persistence, task transitions and Temporal outcomes. Contact tools declare a stable task-scoped logical action key; reads use activation/arguments. Adapters use the supplied scoped transaction and do not commit or control workflows. They cannot overwrite core task fields, operational memory or reserved platform progress.

Adapters are trusted application code, not sandboxed plugins. Their version is saved on executions; incompatible changes need migration or retained old support. The registry currently has one binding per task/input/agent combination. The receipt adapter in tests is not registered in the application.

## Code map

All Python modules below are in `src/agents_platform/`.

| Area | Files |
|---|---|
| HTTP and dashboard | `api.py`, `task_api.py`, `check_in_api.py`, `supervisor_api.py`, `dashboard.py`, `static/` |
| Tasks and storage | `tasks.py`, `models.py`; schema history in root `migrations/` |
| Durable execution | `dispatch.py`, `orchestration/workflows.py`, `orchestration/worker.py` |
| Agent runtime and domains | `execution.py`, `coordination.py`, `domains.py`, `medical_records.py`, `client_check_in.py` |
| Memory and retrieval | `memory.py`, `memory_api.py`, `memory_history.py`, `context.py` |
| Published behavior | `definitions/`, `registry.py`, `seed.py` |

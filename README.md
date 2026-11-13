# HelloCounsel Agents Platform

A working prototype for agent tasks that continue over time. A task is saved, an agent runs briefly, and Temporal waits for a timer or new information before running it again.

The platform supports medical-records collection, scheduled client check-ins, a case supervisor, operator guidance, and shared memory/retrieval. All provider/client communication and document evidence are simulated. Agents can use real OpenAI calls or an explicitly labelled scripted mode.

## Read the docs

| Document | Read it for |
|---|---|
| [Architecture](docs/high-level-design.md) | How the layers work, why they exist, and what each workflow does |
| [Memory and retrieval](docs/memory-and-retrieval.md) | What agents remember, how they request evidence, and how context stays bounded |
| [Development and demos](docs/development.md) | Setup, commands, API entry points, tests, and adding another specialist |
| [Roadmap and decisions](docs/roadmap.md) | What is unfinished, why we chose this design, and research references |

These documents describe the current code. Unbuilt capabilities appear only in the roadmap or explicit limitations, rather than being mixed into the implementation description.

## Start locally

```sh
cp -n .env.example .env
docker compose up --build -d
docker compose run --rm migrate python -m agents_platform.seed
```

Open the [dashboard](http://localhost:8000), [API docs](http://localhost:8000/docs), or [Temporal UI](http://localhost:8233). The default operator key is `local-development-only`; use your configured `OPERATOR_API_KEY` if changed.

Default `AGENT_MODE=scripted` needs no OpenAI key. For real model calls, set `AGENT_MODE=openai` and `OPENAI_API_KEY` in `.env`, then recreate API/worker containers. Provider and client operations remain simulated in either mode.

## What works

- Persistent, parent-linked tasks pinned to published agent versions.
- Durable waiting, event wake-ups, bounded retries, and worker-restart recovery.
- Records collection with evidence checks and duplicate-safe simulated sends.
- One scheduled check-in and attributed reply per task, comparison with the client's prior report, and review of sensitive updates.
- A case supervisor that delegates records work and monitors meaningful client updates.
- Approved instruction revisions, obligations, task/case contact holds, historical keyword/vector search, and question-specific context tools.
- An operator dashboard with task progress, comments, history, tool results, usage and saved context.

Formal approval/takeover, recurring check-in series, temporal fact reconciliation and production integrations remain unfinished. This is a local prototype with a development operator credential, not production firm-user access control.

## Check changes

In an activated host environment installed with `pip install -e '.[dev]'`:

```sh
make check
make test
make smoke
```

`make smoke` requires PostgreSQL and Temporal, creates disposable test databases and isolated workflow queues, and does not clear application data. See [development](docs/development.md) for setup and validation limits.

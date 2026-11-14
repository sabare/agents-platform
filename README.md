# HelloCounsel Agents Platform

A local prototype that helps a law-office teammate assign work, follow progress,
and handle jobs that take days or months. The system saves the job, wakes an agent
when needed, and remembers where it left off.

All client/provider messages and received documents are simulated. Agents can use
scripted responses or real OpenAI model calls; outreach stays simulated in both modes.

## Start here

| Question | Guide |
|---|---|
| How do I assign work and follow it? | [Task assignment](docs/task-assignment.md) |
| What are the parts, and how do they fit together? | [Architecture](docs/high-level-design.md) |
| What do agents remember, and how do they search it? | [Memory and retrieval](docs/memory-and-retrieval.md) |
| What happens when a rule changes or becomes obsolete? | [Memory lifecycle](docs/memory-lifecycle.md) |
| How do I run, test or extend the code? | [Development](docs/development.md) |
| What should we build next? | [Roadmap](docs/roadmap.md) |

## What works today

- One task form, with **Supervisor** selected by default and direct specialist options.
- Supervisor delegation of approved medical-records work into saved subtasks.
- Medical-records collection, evidence checks and up to three simulated reminders.
- One-off client check-ins and bounded recurring series, assigned by a person.
- Client reply comparison and an explicit human-review loop for flagged reports.
- Current rules loaded automatically; historical evidence available through search/read tools.
- Memory review, replacement, resolution, note expiry and instruction retirement, with history retained.
- Saved progress, timers, retry protection and recovery after worker restarts.

**Still to build:** calendar-month schedules that wake the supervisor, and supervisor
delegation to the check-in specialist. The existing recurring series waits an interval
**after completion**; it is not a monthly calendar schedule. See the [next steps](docs/roadmap.md).

## Run locally

```sh
cp -n .env.example .env
docker compose up --build -d
docker compose run --rm migrate python -m agents_platform.seed
```

Open the [dashboard](http://localhost:8000), [API reference](http://localhost:8000/docs)
or [Temporal UI](http://localhost:8233). The default development operator credential
is `local-development-only`. Default scripted mode needs no OpenAI key.

For updates to an existing database, follow the coordinated
[upgrade procedure](docs/development.md#updating-an-existing-stack).
This prototype uses a shared development credential; production user permissions
and real messaging integrations remain unfinished.

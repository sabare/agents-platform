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
| How do monthly check-ins avoid contacting someone twice? | [Monthly check-ins](docs/monthly-check-ins.md) |
| What happens when a rule changes or becomes obsolete? | [Memory lifecycle](docs/memory-lifecycle.md) |
| How do I run, test or extend the code? | [Development](docs/development.md) |
| How does a teammate resolve blocked work? | [Human intervention](docs/human-intervention.md) |
| How do I recover failed work or record a call? | [Recovery and manual contact](docs/recovery-and-contact.md) |
| What should we build next? | [Roadmap](docs/roadmap.md) |

## What works today

- One task form, with **Supervisor** selected by default and direct specialist options.
- Supervisor delegation of approved medical-records work into saved subtasks.
- Per-item medical-records receipt, evidence checks and up to three simulated reminders scoped to missing items.
- Description-first supervisor intake with typed plans, clarification and atomic approval.
- One-off check-ins, fixed-delay series and monthly calendars that wake the supervisor.
- Shared contact-spacing checks that count manual check-ins and client replies.
- Shared human-work queue, audited decisions, owner/deadline labels and same-task resumption.
- Records intervention: authorize one extra follow-up, recheck a blocker or stop collection.
- Client report acknowledgement and typed supervisor objective clarification.
- Current rules loaded automatically; historical evidence available through search/read tools.
- Memory review, replacement, resolution, note expiry and instruction retirement, with history retained.
- Execution incident queue with bounded, audited retry/cancel and fresh-context resumption.
- Structured manual contact, optional provider follow-up and reached-client contact spacing.
- Saved progress, timers, retry protection and recovery after worker restarts.

Monthly calendars keep their date and skip a slot when recent or pending contact
already covers it. The default minimum gap is 28 local calendar days. See
[monthly check-ins](docs/monthly-check-ins.md) for settings and covered cases.
General free-text check-in planning, recorded off-platform contact, broader manual
intervention and real integrations remain in the [roadmap](docs/roadmap.md).

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

See [the interviewer walkthrough review](docs/acceptance-review.md) for live-model journeys,
observed failures, fixes and remaining gaps.

Developer guide: [Adding agents and automatic supervisor discovery](docs/adding-agents.md).

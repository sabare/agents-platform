# Assigning and following work

The example throughout these guides is: **get Ravi's records and check in with him**.
The case groups related work. A task is a saved job card. An activation is one short
turn in which an agent reads the card and works on it.

## Assign through the supervisor

1. Click **Add task**. The **Create task** form opens with **Supervisor** selected.
2. Choose the case and enter the objective.
3. Approve the patient, provider, document types and date range for records collection.
4. Save. The supervisor reviews existing work and proposes missing records subtasks.
5. Application code checks those proposals and starts the permitted specialist tasks.

The supervisor can reuse work, wait for results, ask a person for help, or propose
that the records objective is satisfied. It reviews meaningful outcomes when tasks
finish or get stuck. Satisfying this objective does not close the legal case.

Each case currently has **one supervisor objective**. If one exists, the form loads
it and asks you to acknowledge updating it. Existing specialist tasks continue.
Multiple-provider or mixed-date scopes that the form cannot fully represent are
preserved. The API supports up to ten records targets; the form edits one provider
and a shared date range. **Assign task/Edit supervisor task** on the Supervisor
screen opens the same form.

For a **Records objective**, model-driven delegation remains limited to approved
records targets. Unchecking that scope submits the objective for human review;
it does not enable arbitrary planning. For a calendar, keep Supervisor and choose
**Monthly client check-ins**. The supervisor's deterministic schedule policy checks
recent contact, pending work, reviews and holds before assigning a check-in.
See [monthly check-ins](monthly-check-ins.md).

## Assign a specialist directly

Choose a different value in the same **Agent** selector:

| Agent | What you supply | What it does |
|---|---|---|
| Medical records (v3) | Patient, provider, documents and date scope | Requests simulated records, follows up and verifies saved evidence |
| Client check-in (v1) | Client, send time and message | Sends one simulated check-in, waits for a reply and summarizes it |

Direct tasks can belong to a parent group. Choosing a specialist directly bypasses
the supervisor's initial assignment decision; its meaningful outcomes can still
be monitored by case coordination.

For recurring check-ins, select **Repeat as a bounded check-in series** and set an
interval plus a maximum count or end date. There is one open occurrence at a time.
The next is due after the previous task completes plus the interval. Pause, resume
and stop controls live on the series group. This is not "the fifth of every month."

## Follow progress and give help

Open a task to inspect activity, executions, saved context and tool results.
**Memory & context** shows current items, archived history and evidence search.
You can review an item, replace it or resolve it with a reason. A review date never
lifts a contact hold. See [memory lifecycle](memory-lifecycle.md).

A plain comment is saved. **Wake assigned agent** explicitly wakes the specialist.
`@supervisor` wakes an already configured case coordinator; a mention does not
change the task's assigned agent.

A flagged client reply can block the task. **Allow completion** records that a
teammate handled that report; **Request changes** gives guidance and keeps review
open. The same task resumes. Neither decision clears contact holds or obligations.

The monthly flow is **calendar date → supervisor eligibility check → check-in task
or an explained skip/block**. A recent manual check-in counts toward the default
28-day contact gap. Monthly scheduling does not replace an existing records objective.

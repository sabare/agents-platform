# Describe work to the supervisor

Create task now starts with a case and a description. The supervisor interprets the
request, shows a typed plan and asks for approval before starting work.

Examples:

- “Collect bills and imaging from City Hospital and Valley Clinic for all dates.”
- “Check in with Alex monthly on the fifth at 9am for three months.”
- “Collect records from City Hospital and check in with Alex next week.”

The planner receives only contacts linked to the selected case. Its current
capabilities are records, one-off check-ins, monthly calendars and interval series.
Unknown contacts, ambiguous scope and unsupported actions should yield clarification
questions. Unsupported work cannot be made runnable by a prompt alone.

## Boundaries

- `POST .../cases/{case_id}/assignments/plan` uses an SDK agent with structured output
  and no action tools. It saves the description, plan, assumptions and model usage as
  an `ASSIGNMENT_PLAN` source. Planning creates no tasks or outbox messages.
- `POST .../assignments/{plan_id}/approve` loads the saved plan and rechecks the case,
  participant roles and existing objective revision. The client cannot replace the
  saved payload through this endpoint.
- Approval creates all assignments and dispatch messages in one transaction. Failure
  rolls everything back. Repeated approval returns the original result.
- Records targets go to the existing durable supervisor; it delegates hospital tasks.
  Check-ins reuse the existing durable intake/scheduling services. Compound requests
  can combine them. Existing approved records scopes are preserved.
- Approval is recorded with a source reference and activity event. It never lifts
  contact holds or bypasses domain checks.

## Practical limits

The model can misunderstand descriptions, so the preview displays exact participants,
requirements, dates, cadence, message and assumptions. Schema and scope checks cannot
prove that interpretation matches intent. Keep evaluating real-model examples.

Planning is an interactive request with a 60-second model timeout. A failed request
can be retried; saved plans survive API restarts. OpenAI mode is required for natural
language interpretation. Scripted mode explains the limitation and offers manual
assignment rather than pretending keyword matching is general understanding.

Adding a capability requires a typed plan step, a domain intake handler, planner
instructions, preview presentation and tests. The task execution engine stays shared.

## Task names

The intake planner proposes a business title for the overall assignment and each
hospital target. The coordinator also accepts a specialist title in its delegation
proposal. A directly assigned specialist can name its task on its first successful
turn. Display titles live in task progress separately from the original objective;
renaming does not change instructions, evidence requirements or contact permissions.
Once saved, a title is preserved across subsequent executions.

The interface labels the shared human identity as “User”. Stored attribution and
historical objectives remain unchanged. The simulated-communication notice remains visible.

## Coverage review

Ready proposals receive a separate model review against the complete description.
Unsupported or missing requested work becomes clarification, with no partial dispatch.
The saved plan includes both calls' usage and the coverage verdict. This improves
consistency; model interpretation still needs evaluation and the operator still reviews
the concrete plan. Older unverified drafts must be reviewed again before approval.

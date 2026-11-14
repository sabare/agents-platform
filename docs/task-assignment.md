# Assigning tasks

Use **Create task** for both supervisor-led work and direct specialist assignments.
The **Agent** selector defaults to **Supervisor**. The same form also offers
**Medical records** and **Client check-in** for direct assignment.

For a supervisor task, enter the objective and approve the patient, provider,
required documents and date range. The existing case coordinator proposes
medical-records subtasks, and backend checks validate and save them. Meaningful
specialist outcomes wake the supervisor to review progress, reuse existing work,
request help, create further permitted assignments or verify completion.
Uncheck the approved records scope to submit an objective for human review;
automatic delegation currently supports medical-records collection only.

Each case has one supervisor objective. If it already exists, the form loads it
and requires an explicit acknowledgement before updating that same task. The
current revision protects against overwriting concurrent changes. Existing
specialist tasks continue. Multiple-provider scopes and mixed date ranges are
preserved when the single-provider form cannot represent them faithfully.
The Supervisor overview's assign/edit buttons open this same task form.

Direct specialist assignments keep their existing behavior, including optional
parent tasks, medical-records date scopes, single client check-ins and bounded
check-in series. Comment mentions can wake configured coordination; they do not
change the task's assigned agent.

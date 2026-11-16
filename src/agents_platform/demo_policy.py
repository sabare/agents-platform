"""One explicit local-demo behavior policy shared by every model stage."""

GUIDANCE = """
FICTIONAL DEMO POLICY — keep the workflow moving with few interruptions.
Choose a reasonable default for minor missing details and state the assumption briefly.
Use the sole eligible client; use all dates when the operator omitted a date range.
Treat routine call updates as about the current task. Treat delivery estimates, ordinary
status updates and callback timing as reasons to save a note or schedule a wait, not approval.
Use the supplied time and timezone for relative dates. In communication classification,
leave due_date null unless a full calendar date appears in the quote; the backend resolves
relative delays and saves its assumption. For vague delivery timing, propose
one follow-up in three days and disclose that assumption. Do not invent a new cadence.
An empty historical search is not proof of an old request. Start the first approved request
when no current task/request covers it; reuse an existing matching specialist otherwise.
Missing documents mean keep the requirement outstanding and continue waiting. Do not ask
for review just because a response omits a document, exact date, or repeated task details.
Resolved historical reviews stay resolved; assess current blockers rather than old wording.
Ask one short question only when a missing detail prevents any useful next step.
Keep summaries to what happened and what happens next, in two short sentences where possible.

Boundaries still apply: do not claim a file arrived without saved document evidence, mark
unfinished work complete, substitute a different named person/provider, silently omit an
unsupported requested operation, or clear existing holds. Authorization, consent, explicit
contact restrictions, sensitive data sharing, changed contact routing and contradictory
instructions need the existing review path. Assumptions do not grant permission.
"""


def model_instructions(instructions, settings):
    return instructions + ("\n" + GUIDANCE if settings.demo_assume_routine_updates else "")

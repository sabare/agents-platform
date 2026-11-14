# Adding an agent

A developer adds a module under `src/agents_platform/agent_plugins/` that exports
`CAPABILITIES`. The platform discovers these installed modules automatically. There
is no supervisor agent allowlist or new supervisor dispatch branch to maintain.
`expose_to_supervisor` defaults to `True`.

## Registration

Implement your assignment contract and `DomainAdapter`, then register them in the
same module or import them from your domain module:

```python
from agents_platform.capabilities import AgentCapability
from agents_platform.insurance import InsuranceAdapter, InsuranceAssignment

CAPABILITIES = (
    AgentCapability(
        definition={
            "agent_key": "insurance_follow_up",
            "version": 1,
            "name": "Insurance follow-up",
            "instructions": "Follow the assigned claim; cite tool evidence and escalate disputes.",
            "model": "gpt-4.1-mini",
            "tools": ["read_claim_status", "request_claim_update"],
            "output_schema": "insurance_decision.v1",
            "capability_stage": "durable_demo",
            "limits": {
                "max_model_turns": 8,
                "max_tool_calls": 10,
                "wall_clock_seconds": 300,
            },
        },
        adapter=InsuranceAdapter(),
        assignment_type=InsuranceAssignment,
        description=(
            "Follow up an existing insurance claim. Requires a case insurer contact and "
            "claim reference. Report status and escalate disputes; do not negotiate settlements."
        ),
        # expose_to_supervisor=True is the default. Set False for a private agent.
    ),
)
```

This is a registration example, not a shipped insurance implementation. The tool
names and output schema must match the adapter. The assignment contract extends
`Contract` and rejects extra fields. It must describe domain inputs, not accept
`tenant_id`, `case_id`, parent IDs, workflow identity or agent selection. The backend
supplies that scope. Adapters remain responsible for domain-specific authorization,
relationships, evidence and completion checks.

For a domain whose inputs fit on `Task.input_json`, no task factory is needed. The
default factory persists validated input, a task-created event and an outbox message
in the supervisor's transaction. If a domain needs additional business rows, pass
`create_task=your_async_factory`. Its signature is
`(session, scope: DelegationScope, assignment: YourContract, key: str) -> Task`.
It must use the supplied session without committing, validate domain scope, and
save its business rows plus task/event/outbox atomically. See `agent_plugins/builtins.py`.

## Deploy and discover

Deploy/restart the API and worker as normal after adding Python code. Worker startup
scans the modules and publishes their definitions. `agents-seed` also publishes them
for a fresh demo or test database. Existing published versions cannot be overwritten:
change `version` when changing a definition. JSON definitions in `definitions/` are
also discovered by filename; there is no separate publication list.

On each new supervisor activation, the catalog is checked against the published
snapshots. Only exposed, runnable registrations whose hashes match publication are
shown. Each becomes a typed `delegate_<agent_key>` tool. Its description tells the
supervisor when to use it; its assignment contract supplies the tool schema. A tool
call queues a proposal. After stale-state and scope checks, the backend creates the
child task and outbox record. Temporal executes it independently. Completion and
blocker events wake the supervisor; the supervisor never waits inline for days.

The current catalog supports one selected registration/version per agent key.
Existing tasks retain their pinned agent definition; compatible adapters must remain
available to execute older tasks. Module discovery happens on process startup, not
through hot loading untrusted Python or a user-supplied import path.

## Assignment paths

- **Dashboard:** the assignment planner and its coverage reviewer receive the same
  catalog. A new domain uses an `agent` plan step with typed `assignment_json`. The
  operator reviews its inputs. Approval saves an approved assignment; the supervisor
  subsequently delegates it. Missing or unsupported input still requires clarification.
- **Supervisor API:** submit an objective at
  `POST /v1/firms/{firm}/cases/{case}/supervisor`. In OpenAI mode, the supervisor can
  choose from registered tools using the objective and supplied case contacts.
- **Deterministic/offline path:** include `approved_assignments` in the objective:

```json
{
  "objective": "Follow up claim CL-17 with the case insurer.",
  "approved_assignments": [{
    "agent_key": "insurance_follow_up",
    "agent_version": 1,
    "objective": "Obtain the current status of claim CL-17.",
    "assignment": {"insurer_contact_id": "<case-contact-uuid>", "claim_reference": "CL-17"}
  }]
}
```

The scripted supervisor delegates approved typed assignments; it does not pretend to
understand arbitrary new natural-language requests. A model is required for that.

Inspect discovery at
`GET /v1/firms/{firm}/cases/{case}/supervisor/capabilities` using the normal operator
credential. Hidden/unpublished agents are absent and cannot be selected by a forged
supervisor proposal.

New or explicitly revised objectives use supervisor v3. Existing v1/v2 objectives
retain their pinned behavior until explicitly revised with `expected_revision`.
This does not silently rewrite running assignments. To expose a new agent, no further
supervisor code changes are necessary.

## Verification

`tests/test_agent_capabilities.py` covers module discovery, default exposure and
opt-out, generated typed tools, publication/version mismatches, duplicate delegation,
foreign-case rejection, factory-backed creation, assignment planning/approval and a
third agent's complete supervisor-to-worker-to-supervisor journey. These use disposable
PostgreSQL databases and controlled model responses; they do not evaluate natural-language
routing accuracy against a live model.

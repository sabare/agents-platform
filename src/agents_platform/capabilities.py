"""Developer-owned agent discovery and the common supervisor delegation contract."""

import importlib
import pkgutil
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import select

from agents_platform.domains import AdapterRegistry, DomainAdapter
from agents_platform.models import (
    ActivityEvent,
    AgentVersion,
    Case,
    CaseContact,
    OutboxMessage,
    Task,
)
from agents_platform.registry import definition_hash
from agents_platform.task_contracts import Contract

# Assignment data cannot choose execution scope or implementation.
SCOPE_FIELDS = {
    "tenant_id",
    "firm_id",
    "case_id",
    "parent_task_id",
    "agent_key",
    "agent_version",
    "workflow_id",
    "task_type",
    "input_schema",
}


@dataclass(frozen=True)
class DelegationScope:
    tenant_id: UUID
    case_id: UUID
    parent_task_id: UUID
    objective: str
    agent_version: int
    actor_ref: str


@dataclass(frozen=True)
class AgentCapability:
    definition: dict
    adapter: DomainAdapter
    assignment_type: type[Contract]
    description: str
    expose_to_supervisor: bool = True
    # Optional for domains needing business rows. Default stores typed input on Task.
    create_task: Callable[..., Awaitable[Task]] | None = None

    def __post_init__(self):
        key = self.definition["agent_key"]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,54}", key):
            raise ValueError("Agent key must be a short lowercase Python identifier")
        if not self.description.strip() or not issubclass(self.assignment_type, Contract):
            raise ValueError("Capabilities require a description and typed assignment contract")
        if SCOPE_FIELDS.intersection(self.assignment_type.model_fields):
            raise ValueError("Assignment contract must not accept platform scope fields")
        if self.assignment_type.model_config.get("extra") != "forbid":
            raise ValueError("Assignment contracts must reject unknown fields")
        AdapterRegistry((self.adapter,)).for_execution(
            SimpleNamespace(
                task_type=self.adapter.task_type,
                input_schema=self.adapter.input_schema,
                agent_key=key,
                agent_version=self.definition["version"],
            ),
            self.definition,
        )

    @property
    def key(self):
        return self.definition["agent_key"]

    def card(self):
        return {
            "agent_key": self.key,
            "version": self.definition["version"],
            "name": self.definition["name"],
            "description": self.description,
            "task_type": self.adapter.task_type,
            "input_schema": self.adapter.input_schema,
            "assignment_schema": self.assignment_type.model_json_schema(),
            "definition_hash": definition_hash(self.definition),
            "adapter": {"key": self.adapter.key, "version": self.adapter.version},
            "expose_to_supervisor": self.expose_to_supervisor,
        }


class CapabilityCatalog:
    def __init__(self, capabilities):
        self.capabilities = {}
        adapters = {}
        for capability in capabilities:
            if capability.key in self.capabilities:
                raise ValueError(f"Duplicate agent capability: {capability.key}")
            self.capabilities[capability.key] = capability
            binding = (
                capability.adapter.task_type,
                capability.adapter.input_schema,
                capability.adapter.agent_key,
            )
            if binding in adapters and adapters[binding] is not capability.adapter:
                raise ValueError("Conflicting adapters for one task binding")
            adapters[binding] = capability.adapter
        self.adapters = AdapterRegistry(tuple(adapters.values()))

    async def available(self, session):
        """Only runnable, exposed registrations matching immutable published definitions."""
        published = {
            (row.agent_key, row.version): row
            for row in (await session.scalars(select(AgentVersion))).all()
        }
        cards = []
        for capability in self.capabilities.values():
            definition = capability.definition
            row = published.get((capability.key, definition["version"]))
            if (
                capability.expose_to_supervisor
                and definition.get("capability_stage") == "durable_demo"
                and row is not None
                and row.definition_hash == definition_hash(definition)
                and definition_hash(row.definition_snapshot) == row.definition_hash
            ):
                cards.append(capability.card())
        return sorted(cards, key=lambda c: c["agent_key"])

    def resolve(self, key, version):
        capability = self.capabilities.get(key)
        if (
            capability is None
            or not capability.expose_to_supervisor
            or capability.definition["version"] != version
        ):
            raise ValueError("Agent is not an exposed registered capability")
        return capability


def discover_capabilities(package_name="agents_platform.agent_plugins"):
    """Scan installed, developer-owned modules; never import model/user-supplied paths."""
    package = importlib.import_module(package_name)
    capabilities = []
    for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module.name.startswith("_"):
            continue
        plugin = importlib.import_module(f"{package_name}.{module.name}")
        entries = getattr(plugin, "CAPABILITIES", ())
        if not isinstance(entries, (tuple, list)) or any(
            not isinstance(entry, AgentCapability) for entry in entries
        ):
            raise ValueError(f"Invalid CAPABILITIES in {module.name}")
        capabilities.extend(entries)
    return CapabilityCatalog(capabilities)


async def delegate_task(session, catalog, card, scope, assignment):
    """Called inside supervisor commit: validate, deduplicate, save task + wake atomically."""
    from agents_platform.coordination import digest
    from agents_platform.tasks import TaskError

    capability = catalog.resolve(card["agent_key"], card["version"])
    if capability.card() != card or card not in await catalog.available(session):
        raise ValueError("Capability changed or is no longer published/exposed")
    if scope.agent_version != card["version"]:
        raise ValueError("Delegation scope must pin the advertised agent version")
    case = await session.scalar(
        select(Case)
        .where(
            Case.tenant_id == scope.tenant_id,
            Case.id == scope.case_id,
        )
        .with_for_update()
    )
    parent = await session.scalar(
        select(Task).where(
            Task.tenant_id == scope.tenant_id,
            Task.case_id == scope.case_id,
            Task.id == scope.parent_task_id,
            Task.kind == "GROUP",
            Task.status.not_in(["COMPLETED", "CANCELLED", "FAILED"]),
        )
    )
    if not case or case.status != "OPEN" or parent is None:
        raise TaskError(422, "Delegation requires an open case and grouping task in the same firm")
    arguments = capability.assignment_type.model_validate(assignment)
    payload = arguments.model_dump(mode="json")

    # Generic assignments may reference case contacts, never contacts from another case.
    async def validate_contacts(value):
        if isinstance(value, dict):
            for field, item in value.items():
                if field.endswith("contact_id") and item is not None:
                    if not await session.scalar(
                        select(CaseContact.contact_id).where(
                            CaseContact.tenant_id == scope.tenant_id,
                            CaseContact.case_id == scope.case_id,
                            CaseContact.contact_id == UUID(str(item)),
                        )
                    ):
                        raise TaskError(422, "Assignment contact must belong to this case")
                await validate_contacts(item)
        elif isinstance(value, list):
            for item in value:
                await validate_contacts(item)

    await validate_contacts(payload)
    key = "delegation/" + digest(
        {
            "case": str(scope.case_id),
            "parent": str(scope.parent_task_id),
            "agent": capability.key,
            "version": scope.agent_version,
            "assignment": payload,
        }
    )
    existing = await session.scalar(
        select(Task).where(
            Task.tenant_id == scope.tenant_id,
            Task.creation_key == key,
        )
    )
    ledger = await session.scalar(
        select(ActivityEvent).where(
            ActivityEvent.tenant_id == scope.tenant_id,
            ActivityEvent.producer == "supervisor-delegation",
            ActivityEvent.dedupe_key == key,
        )
    )
    if ledger:
        existing = await session.get(Task, ledger.task_id)
    if existing:
        if existing.status == "CANCELLED":
            raise TaskError(409, "Cancelled delegation requires an explicit new assignment")
        return existing, False
    if capability.create_task:
        task = await capability.create_task(session, scope, arguments, key)
    else:
        task_id, event_id = uuid4(), uuid4()
        task = Task(
            id=task_id,
            tenant_id=scope.tenant_id,
            case_id=scope.case_id,
            parent_task_id=scope.parent_task_id,
            kind="AGENT",
            task_type=capability.adapter.task_type,
            objective=scope.objective,
            agent_key=capability.key,
            agent_version=scope.agent_version,
            input_schema=capability.adapter.input_schema,
            input_json=payload,
            progress_json={},
            completion_json={},
            status="PENDING",
            workflow_id=f"firm/{scope.tenant_id}/task/{task_id}",
            creation_key=key,
            creation_hash=digest(payload),
        )
        session.add(task)
        await session.flush()
        session.add(
            ActivityEvent(
                id=event_id,
                tenant_id=scope.tenant_id,
                case_id=scope.case_id,
                task_id=task.id,
                event_type="task.created",
                producer="supervisor-delegation",
                dedupe_key=key,
                actor_ref=scope.actor_ref,
                payload={"agent_key": capability.key, "agent_version": scope.agent_version},
            )
        )
        await session.flush()
        session.add(
            OutboxMessage(
                tenant_id=scope.tenant_id,
                event_id=event_id,
                destination="temporal.start_task",
                destination_key=task.workflow_id,
                payload={"tenant_id": str(scope.tenant_id), "task_id": str(task.id)},
            )
        )
    if (
        task.tenant_id != scope.tenant_id
        or task.case_id != scope.case_id
        or task.parent_task_id != scope.parent_task_id
        or task.agent_key != capability.key
        or task.agent_version != scope.agent_version
    ):
        raise ValueError("Capability factory returned a task outside delegation scope")
    task.progress_json = {
        **task.progress_json,
        "supervisor_delegation": {"agent_key": capability.key, "assignment": payload},
    }
    # Factory-backed domains use their own creation key; this event is the common dedupe ledger.
    if capability.create_task:
        session.add(
            ActivityEvent(
                tenant_id=scope.tenant_id,
                case_id=scope.case_id,
                task_id=task.id,
                event_type="task.delegated",
                producer="supervisor-delegation",
                dedupe_key=key,
                actor_ref=scope.actor_ref,
                payload={"agent_key": capability.key},
            )
        )
    await session.flush()
    return task, True

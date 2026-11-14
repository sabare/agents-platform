"""Code-owned domain boundary. Adapters supply business rules, never workflow control."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from agents import Model
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.models import Task
from agents_platform.task_contracts import AgentDecision, Contract, NextAction


class EmptyArguments(Contract):
    pass


class ActionStateChanged(ValueError):
    """Current business state invalidates a proposed action; reload before deciding."""


class DecisionValidationError(ValueError):
    """A model proposal needs correction; this is not an infrastructure failure."""


class ActionNotDue(ValueError):
    """A rejected premature action can safely return the task to its durable timer."""

    def __init__(self, ready_at: datetime):
        self.ready_at = ready_at
        super().__init__(f"Action is not due until {ready_at.isoformat()}; no action was taken")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    effect: Literal["read", "contact"]
    arguments: type[Contract] = EmptyArguments
    task_key: str | Callable[[Contract], str] | None = (
        None  # A logical action that must happen once across activations.
    )


@dataclass
class ToolResult:
    data: dict
    evidence_source_ids: tuple[UUID, ...] = ()
    source_kind: str = "TOOL_RESULT"
    event_type: str | None = None


@dataclass
class DomainSnapshot:
    state: dict
    ready_at: datetime | None = None
    instruction_provider_ids: tuple[UUID, ...] = ()
    task_fields: dict = field(default_factory=dict)  # Compatibility projection for current API/UI.
    action_requirements: dict = field(default_factory=dict)
    evidence_source_ids: tuple[UUID, ...] = ()


@dataclass
class DomainValidation:
    action: NextAction
    progress: dict = field(default_factory=dict)
    replan_reason: str | None = None
    rejected: bool = False
    notify_supervisor: bool = True


class DomainAdapter(Protocol):
    key: str
    version: int
    task_type: str
    input_schema: str
    agent_key: str
    output_schemas: tuple[str, ...]
    decision_type: type[AgentDecision]
    tools: tuple[ToolSpec, ...]

    async def load(self, session: AsyncSession, task: Task) -> DomainSnapshot: ...

    async def execute_tool(
        self, session: AsyncSession, task: Task, name: str, arguments: Contract
    ) -> ToolResult: ...

    async def validate(
        self,
        session: AsyncSession,
        task: Task,
        context: dict,
        decision: AgentDecision,
        results: list[dict],
        action: NextAction,
    ) -> DomainValidation: ...

    def review(self, reason: str) -> AgentDecision: ...

    def scripted_model(self, context: dict, follow_up_seconds: int) -> Model: ...


def adapter_ref(adapter: DomainAdapter):
    return {"key": adapter.key, "version": adapter.version}


class AdapterRegistry:
    """Explicit application-owned bindings. No imports or adapter names supplied by an LLM."""

    def __init__(self, adapters: tuple[DomainAdapter, ...]):
        self.bindings = {}
        self.identities = set()
        for adapter in adapters:
            binding = (adapter.task_type, adapter.input_schema, adapter.agent_key)
            identity = (adapter.key, adapter.version)
            if binding in self.bindings or identity in self.identities:
                raise ValueError("Duplicate domain adapter binding or identity")
            if adapter.version < 1 or not issubclass(adapter.decision_type, AgentDecision):
                raise ValueError("Invalid adapter version or decision type")
            names = [t.name for t in adapter.tools]
            if len(set(names)) != len(names):
                raise ValueError("Duplicate domain tool")
            for spec in adapter.tools:
                if spec.effect not in ("read", "contact"):
                    raise ValueError("Every tool must declare its effect")
                if spec.effect == "contact" and not spec.task_key:
                    raise ValueError("Contact tools require a task-scoped logical action key")
            self.bindings[binding] = adapter
            self.identities.add(identity)

    def for_task(self, task: Task) -> DomainAdapter:
        binding = (task.task_type, task.input_schema, task.agent_key)
        if binding not in self.bindings:
            raise ValueError(f"Unsupported task domain: {task.task_type}/{task.input_schema}")
        return self.bindings[binding]

    def for_execution(self, task: Task, definition: dict, saved_ref=None) -> DomainAdapter:
        adapter = self.for_task(task)
        if definition["agent_key"] != task.agent_key or definition["version"] != task.agent_version:
            raise ValueError("Published agent does not match task assignment")
        if definition["output_schema"] not in adapter.output_schemas:
            raise ValueError("Published output schema is not supported by the domain")
        allowed = [t.name for t in adapter.tools]
        if len(set(definition["tools"])) != len(definition["tools"]):
            raise ValueError("Published agent has duplicate tools")
        if any(name not in allowed for name in definition["tools"]):
            raise ValueError("Published tool is not supported by the domain")
        if saved_ref is not None and saved_ref != adapter_ref(adapter):
            raise ValueError("Saved activation requires a different domain adapter version")
        return adapter


def default_registry():
    from agents_platform.capabilities import discover_capabilities

    return discover_capabilities().adapters

"""Shared OpenTelemetry/Langfuse tracing. Exported traces exclude raw case text."""

import base64
import functools
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from agents import RunHooks, Runner
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from sqlalchemy import event
from sqlalchemy.orm import Session

LOG = logging.getLogger("agents_platform.observability")
_IDS = ContextVar("trace_identity", default={})
_SPANS = ContextVar("platform_spans", default=())
_AI = ContextVar("ai_execution", default=False)
PROPAGATOR = TraceContextTextMapPropagator()

# Free text, arbitrary payload keys, clinical values, SQL parameters and URL queries
# are deliberately not captured. This is stricter than regex-only PII masking.
PAYLOAD_KEYS = {
    "role",
    "type",
    "content",
    "text",
    "name",
    "arguments",
    "output",
    "input",
    "summary",
    "observations",
    "next_action",
    "reason",
    "task",
    "domain",
    "memory",
    "history",
    "sources",
    "instructions",
    "new_events",
    "recent_events",
    "action_requirements",
    "status",
    "state",
    "task_type",
    "tool_calls",
    "function",
    "call_id",
    "source_id",
    "record_type",
    "occurrence",
    "follow_up_seconds",
    "task_title",
    "report_id",
    "received_source_ids",
    "error",
    "assignment",
    "capabilities",
    "contacts",
    "objective",
    "plan",
    "deadline",
    "expected_version",
}
CONTROL_VALUES = {
    "system",
    "user",
    "assistant",
    "developer",
    "tool",
    "message",
    "input_text",
    "output_text",
    "function_call",
    "function_call_output",
    "PENDING",
    "RUNNING",
    "WAITING",
    "BLOCKED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "STALE",
    "SUCCEEDED",
    "CONTINUE",
    "wait",
    "complete",
    "needs_human",
    "RECORDS",
    "BILLS",
    "IMAGING",
    "read",
    "contact",
}
SAFE_ATTRIBUTES = {
    "case.id",
    "task.id",
    "execution.id",
    "tenant.id",
    "agent.key",
    "agent.version",
    "agent.definition_hash",
    "prompt.version",
    "prompt.hash",
    "runtime.mode",
    "execution.cycle",
    "execution.attempt",
    "execution.trigger",
    "execution.status",
    "tool.name",
    "tool.cached",
    "tool.rejected",
    "context.source_count",
    "context.event_count",
    "context.item_count",
    "error.type",
    "temporal.workflow.id",
    "temporal.run.id",
    "temporal.activity.id",
    "temporal.activity.type",
    "temporal.attempt",
    "temporal.queue_delay_ms",
    "temporalWorkflowID",
    "temporalRunID",
    "temporalActivityID",
    "temporalActivityType",
    "platform.ai_trace",
    "redaction.applied",
    "langfuse.observation.type",
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.model.name",
    "langfuse.observation.usage_details",
    "langfuse.observation.cost_details",
    "langfuse.version",
    "langfuse.trace.name",
    "session.id",
    "langfuse.environment",
    "langfuse.release",
    "langfuse.internal.as_root",
    "langfuse.internal.is_app_root",
    "gen_ai.request.model",
    "gen_ai.provider.name",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "http.request.method",
    "http.method",
    "http.response.status_code",
    "http.status_code",
    "http.route",
    "db.system",
    "db.system.name",
    "db.operation.name",
    "db.operation",
}


def redact_payload(value, *, tool_names=(), _depth=0):
    if _depth > 8:
        return "[REDACTED]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key if key in PAYLOAD_KEYS else f"redacted_field_{index}": redact_payload(
                item, tool_names=tool_names
            )
            for index, (key, item) in enumerate(list(value.items())[:100])
        }
    if isinstance(value, (list, tuple)):
        return [
            redact_payload(item, tool_names=tool_names, _depth=_depth + 1) for item in value[:100]
        ]
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if len(value) <= 20000 else None
            if isinstance(parsed, (dict, list)):
                return redact_payload(parsed, tool_names=tool_names, _depth=_depth + 1)
        except (ValueError, TypeError):
            pass
        return value if value in CONTROL_VALUES or value in tool_names else "[REDACTED]"
    if value is None:
        return None
    return "[REDACTED]"


def debug_payload(value):
    """Readable local model content, with credentials removed at both boundaries."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: "[REDACTED CREDENTIAL]"
            if re.fullmatch(
                r"(?:password|secret|authorization|api[_-]?key|access[_-]?token|credential)",
                key,
                re.I,
            )
            else debug_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [debug_payload(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (dict, list, str)) and parsed != value:
                return debug_payload(parsed)
        except (ValueError, TypeError):
            pass
        value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED CREDENTIAL]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [REDACTED CREDENTIAL]", value)
        return re.sub(
            r"(?i)(password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED CREDENTIAL]",
            value,
        )
    return value


def trace_payload(value, **kwargs):
    settings = _runtime.settings
    return (
        debug_payload(value)
        if settings and settings.langfuse_content_mode == "local_debug"
        else redact_payload(value, **kwargs)
    )


def safe_span(span, *, readable=False):
    attributes = {k: v for k, v in (span.attributes or {}).items() if k in SAFE_ATTRIBUTES}
    for key in ("langfuse.observation.input", "langfuse.observation.output"):
        if key in attributes:
            attributes[key] = json.dumps(
                debug_payload(attributes[key])
                if readable
                else redact_payload(attributes[key], tool_names=(attributes.get("tool.name", ""),))
            )
    attributes["redaction.applied"] = not readable
    scope = span.instrumentation_scope
    name = span.name
    if scope and "sqlalchemy" in scope.name:
        name = "db.operation"
    elif scope and ("fastapi" in scope.name or "asgi" in scope.name):
        name = "http.request"
    resource = Resource(
        {
            k: v
            for k, v in span.resource.attributes.items()
            if k in {"service.name", "service.version", "deployment.environment.name"}
        }
    )
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=resource,
        attributes=attributes,
        events=(),
        links=[Link(link.context) for link in span.links],
        kind=span.kind,
        instrumentation_scope=scope,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
    )


class SafeExporter(SpanExporter):
    """The same fail-closed redaction boundary protects every export destination."""

    def __init__(self, exporter, *, readable=False):
        self.exporter = exporter
        self.readable = readable

    def export(self, spans):
        try:
            return self.exporter.export(
                tuple(safe_span(span, readable=self.readable) for span in spans)
            )
        except Exception as exc:
            LOG.warning("Trace export failed (%s)", type(exc).__name__)
            return SpanExportResult.FAILURE

    def shutdown(self):
        self.exporter.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.exporter.force_flush(timeout_millis)


class IdentityProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        for key, value in _IDS.get().items():
            if key not in (span.attributes or {}):
                span.set_attribute(key, value)
        if _AI.get():
            span.set_attribute("platform.ai_trace", True)


@dataclass
class Observability:
    provider: object = None
    langfuse: object = None
    settings: object = None
    closed: bool = False

    @property
    def enabled(self):
        return self.provider is not None

    def flush(self):
        if self.langfuse:
            self.langfuse.flush()
        if self.provider:
            self.provider.force_flush(timeout_millis=5000)

    def shutdown(self):
        global _runtime
        if self.closed:
            return
        self.closed = True
        self.flush()
        if self.langfuse:
            self.langfuse.shutdown()
        if self.provider:
            self.provider.shutdown()
        if _runtime is self:
            _runtime = Observability()


_runtime = Observability()


def runtime():
    return _runtime


def configure_observability(settings, service="worker", *, exporter=None):
    global _runtime
    if not settings.otel_enabled:
        return Observability(settings=settings)
    if _runtime.enabled:
        return _runtime
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": f"hellocounsel-{service}",
                "service.version": settings.service_version,
                "deployment.environment.name": settings.otel_environment,
            }
        )
    )
    provider.add_span_processor(IdentityProcessor())
    endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/")
    if exporter or endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                SafeExporter(
                    exporter or OTLPSpanExporter(endpoint=endpoint + "/v1/traces", timeout=3)
                ),
                schedule_delay_millis=500,
            )
        )
    langfuse = None
    if settings.langfuse_public_key and settings.langfuse_secret_key.get_secret_value():
        from langfuse import Langfuse

        auth = base64.b64encode(
            (
                settings.langfuse_public_key + ":" + settings.langfuse_secret_key.get_secret_value()
            ).encode()
        ).decode()
        langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            environment=settings.otel_environment,
            release=settings.service_version,
            tracer_provider=provider,
            mask=lambda *, data, **kwargs: trace_payload(data),
            should_export_span=lambda span: bool((span.attributes or {}).get("platform.ai_trace")),
            span_exporter=SafeExporter(
                OTLPSpanExporter(
                    endpoint=settings.langfuse_base_url.rstrip("/") + "/api/public/otel/v1/traces",
                    headers={"Authorization": "Basic " + auth, "x-langfuse-ingestion-version": "4"},
                    timeout=3,
                ),
                readable=settings.langfuse_content_mode == "local_debug",
            ),
        )
    _runtime = Observability(provider=provider, langfuse=langfuse, settings=settings)
    return _runtime


def tracer():
    provider = _runtime.provider or trace.NoOpTracerProvider()
    return provider.get_tracer("agents_platform")


def bind_identity(**fields):
    current = {
        **_IDS.get(),
        **{
            k: str(v)
            if k == "prompt.version"
            else v
            if isinstance(v, (str, int, float, bool))
            else str(v)
            for k, v in fields.items()
            if v is not None
        },
    }
    _IDS.set(current)
    for active in _SPANS.get():
        for key, value in current.items():
            active.set_attribute(key, value)
        if current.get("case.id"):
            active.set_attribute("session.id", current["case.id"])


@contextmanager
def observation(
    name, *, attributes=None, carrier=None, ai=False, kind="span", span_kind=SpanKind.INTERNAL
):
    context = PROPAGATOR.extract(carrier) if carrier else None
    token = _AI.set(_AI.get() or ai)
    attrs = {"langfuse.observation.type": kind, **(attributes or {})}
    if ai:
        attrs["platform.ai_trace"] = True
    with tracer().start_as_current_span(
        name,
        context=context,
        kind=span_kind,
        attributes=attrs,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        stack_token = _SPANS.set((*_SPANS.get(), current))
        try:
            yield current
        except BaseException as exc:
            current.set_attribute("error.type", type(exc).__name__)
            current.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            _SPANS.reset(stack_token)
            _AI.reset(token)


def phase(name):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            with observation(name) as current:
                result = await function(*args, **kwargs)
                if isinstance(result, dict):
                    current.set_attribute(
                        "langfuse.observation.output", json.dumps(trace_payload(result))
                    )
                return result

        return wrapped

    return decorate


def observed_tool(name, callback):
    @functools.wraps(callback)
    async def wrapped(context, raw_arguments):
        with observation("tool." + name, kind="tool", attributes={"tool.name": name}) as current:
            current.set_attribute(
                "langfuse.observation.input",
                json.dumps(trace_payload(raw_arguments, tool_names=(name,))),
            )
            from agents_platform.audit import invoke_tool

            result = await invoke_tool(name, callback, context, raw_arguments)
            current.set_attribute(
                "langfuse.observation.output",
                json.dumps(trace_payload(result, tool_names=(name,))),
            )
            return result

    return wrapped


def trace_reference():
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    trace_id = f"{context.trace_id:032x}"
    settings = _runtime.settings
    execution_span = next(
        (s for s in reversed(_SPANS.get()) if getattr(s, "name", None) == "agent.execution"), None
    )
    return {
        "execution_span_id": f"{execution_span.get_span_context().span_id:016x}"
        if execution_span
        else f"{context.span_id:016x}",
        "trace_id": trace_id,
        "span_id": f"{context.span_id:016x}",
        "jaeger_url": (settings.jaeger_ui_url.rstrip("/") + "/trace/" + trace_id)
        if settings and settings.otel_exporter_otlp_endpoint
        else None,
        "langfuse_url": (
            settings.langfuse_ui_url.rstrip("/")
            + "/project/"
            + settings.langfuse_project_id
            + "/traces/"
            + trace_id
        )
        if _runtime.langfuse
        else None,
    }


class ModelTraceHooks(RunHooks):
    def __init__(self, definition, mode):
        self.definition, self.mode, self.pending = definition, mode, []

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        import hashlib

        model = self.definition.get("model", str(agent.model))
        current = tracer().start_span(
            "llm.call",
            kind=SpanKind.CLIENT if self.mode == "openai" else SpanKind.INTERNAL,
            attributes={
                "langfuse.observation.type": "generation",
                "platform.ai_trace": True,
                "langfuse.observation.model.name": model,
                "gen_ai.request.model": model,
                "gen_ai.provider.name": "openai" if self.mode == "openai" else "scripted",
                "runtime.mode": self.mode,
                "agent.key": self.definition.get("agent_key", agent.name),
                "agent.version": self.definition.get("version", 0),
                "prompt.version": str(self.definition.get("version", 0)),
                "prompt.hash": hashlib.sha256((system_prompt or "").encode()).hexdigest(),
                "langfuse.version": str(self.definition.get("version", 0)),
                "langfuse.observation.input": json.dumps(
                    trace_payload(
                        {"instructions": system_prompt, "input": input_items},
                        tool_names=tuple(tool.name for tool in agent.tools),
                    )
                ),
            },
        )
        self.pending.append(current)

    async def on_llm_end(self, context, agent, response):
        if not self.pending:
            return
        current = self.pending.pop()
        current.set_attribute(
            "langfuse.observation.output",
            json.dumps(
                trace_payload(
                    response.output,
                    tool_names=tuple(tool.name for tool in agent.tools),
                )
            ),
        )
        usage = response.usage
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            current.set_attribute("gen_ai.usage." + name, getattr(usage, name, 0))
        current.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps(
                {
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                    "total": usage.total_tokens,
                }
            ),
        )
        if self.mode != "openai":
            current.set_attribute("langfuse.observation.cost_details", json.dumps({"total": 0}))
        current.end()

    def finish(self, error=None):
        for current in self.pending:
            current.set_status(Status(StatusCode.ERROR))
            current.set_attribute(
                "error.type", type(error).__name__ if error else "IncompleteModelCall"
            )
            current.end()
        self.pending.clear()


async def run_agent(agent, input, *, definition=None, mode="openai", **kwargs):
    from agents_platform.registry import definition_hash

    hooks = ModelTraceHooks(definition or {}, mode)
    if definition:
        bind_identity(
            **{
                "agent.key": definition.get("agent_key"),
                "agent.version": definition.get("version"),
                "prompt.version": definition.get("version"),
                "agent.definition_hash": definition_hash(definition),
            }
        )
    with observation("agent.run", ai=True, kind="agent"):
        error = None
        try:
            return await Runner.run(agent, input, hooks=hooks, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            hooks.finish(error)


def evaluation_scores(outcome, usage=None):
    """Backend checks, not an invented score of clinical or language-model quality."""
    if (usage or {}).get("context_budget_exceeded"):
        return  # No validated business decision was produced.
    if not _runtime.langfuse or outcome.get("status") not in {
        "WAITING",
        "BLOCKED",
        "COMPLETED",
        "FAILED",
    }:
        return
    reference = trace_reference()
    if not reference:
        return
    for name, value in {
        "backend_validation_passed": not bool((usage or {}).get("decision_validation_error")),
        "task_completed": outcome.get("status") == "COMPLETED",
    }.items():
        try:
            _runtime.langfuse.create_score(
                trace_id=reference["trace_id"],
                observation_id=reference["execution_span_id"],
                name=name,
                value=int(value),
                data_type="BOOLEAN",
                metadata={"source": "backend", **_IDS.get()},
            )
        except Exception as exc:
            LOG.warning("Evaluation export failed (%s)", type(exc).__name__)


@event.listens_for(Session, "before_flush")
def propagate_outbox_context(session, flush_context, instances):
    from agents_platform.models import OutboxMessage

    carrier = {}
    PROPAGATOR.inject(carrier)
    if not carrier:
        return
    for row in session.new:
        if isinstance(row, OutboxMessage):
            row.payload = {**row.payload, "_trace": carrier}


@contextmanager
def activity_trace(fields):
    token = _IDS.set({})
    bind_identity(
        **{
            "tenant.id": fields.get("firm_id"),
            "case.id": fields.get("case_id"),
            "task.id": fields.get("task_id"),
            "execution.cycle": fields.get("cycle"),
            "execution.trigger": fields.get("trigger"),
            "temporal.workflow.id": fields.get("workflow_id"),
            "temporal.run.id": fields.get("run_id"),
            "temporal.activity.id": fields.get("activity_id"),
            "temporal.activity.type": fields.get("activity_type"),
            "temporal.attempt": fields.get("attempt"),
            "temporal.queue_delay_ms": fields.get("schedule_to_start_ms"),
        }
    )
    try:
        with (
            observation("case", ai=True),
            observation("task"),
            observation("agent.execution", kind="agent") as execution,
        ):
            execution.set_attribute("langfuse.trace.name", "Agent execution")
            yield execution
    finally:
        _IDS.reset(token)


def record_context(context):
    current = trace.get_current_span()
    for key, value in {
        "context.source_count": len(context.get("sources", [])),
        "context.event_count": len(context.get("new_events", [])),
        "context.item_count": len(context.get("memory", {}).get("operational_items", [])),
    }.items():
        current.set_attribute(key, value)
    current.set_attribute("langfuse.observation.input", json.dumps(trace_payload(context)))


def instrument_database(engine):
    """Public SQLAlchemy events work with the deployed SQLAlchemy 2.1 runtime.

    Only the operation kind is recorded: never statements, parameters or exception text.
    """

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if not runtime().enabled:
            return
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "QUERY"
        if operation not in {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "ROLLBACK"}:
            operation = "QUERY"
        context._platform_span = tracer().start_span(
            "db.operation",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system.name": "postgresql",
                "db.operation.name": operation,
            },
        )

    def after_execute(connection, cursor, statement, parameters, context, executemany):
        current = getattr(context, "_platform_span", None)
        if current:
            current.end()
            context._platform_span = None

    def failed(exception_context):
        context = exception_context.execution_context
        current = getattr(context, "_platform_span", None)
        if current:
            current.set_attribute("error.type", type(exception_context.original_exception).__name__)
            current.set_status(Status(StatusCode.ERROR))
            current.end()
            context._platform_span = None

    event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
    event.listen(engine.sync_engine, "after_cursor_execute", after_execute)
    event.listen(engine.sync_engine, "handle_error", failed)


def outbox_payload(payload):
    carrier = {}
    PROPAGATOR.inject(carrier)
    return {**payload, "_trace": carrier} if carrier else payload

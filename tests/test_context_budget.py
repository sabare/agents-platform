"""Budget behavior under large histories and growing SDK tool conversations."""

import json
from types import SimpleNamespace

import pytest
from agents import Agent
from agents.run import CallModelData, ModelInputData
from pydantic import BaseModel, ValidationError

from agents_platform.config import Settings
from agents_platform.context import (
    ContextBudgetExceeded,
    ModelBudget,
    RetrievalRequest,
)


class Answer(BaseModel):
    answer: str


def data(context, extra=None):
    agent = Agent(name="test", instructions="Use evidence", output_type=Answer)
    return CallModelData(
        model_data=ModelInputData(
            input=[{"role": "user", "content": json.dumps(context)}, *(extra or [])],
            instructions=agent.instructions,
        ),
        agent=agent,
        context=None,
    )


def test_budget_removes_optional_history_preserves_old_hold_and_original_snapshot():
    context = {
        "task": {"objective": "Find missing MRI"},
        "instructions": [{"text": "Do not contact"}],
        "memory": {
            "instructions": [{"text": "Do not contact"}],
            "operational_items": [{"kind": "STOP_CONTACT", "text": "Old hold"}],
        },
        "history": {
            "retrieval": {
                "results": [{"text": "Irrelevant historical material " * 1000} for _ in range(20)]
            }
        },
    }
    budget = ModelBudget(context, Settings(_env_file=None, context_input_tokens=3000))
    result = budget.filter(data(context))
    packed = json.loads(result.input[0]["content"])
    assert packed["memory"] == context["memory"]
    assert "instructions" not in packed
    assert packed["context_selection"]["truncated"]
    assert len(context["history"]["retrieval"]["results"]) == 20
    assert budget.calls[-1] <= 3000


def test_budget_does_not_silently_drop_essential_task_state():
    context = {"task": {"objective": "Very long responsibility " * 10000}, "memory": {}}
    with pytest.raises(ContextBudgetExceeded):
        ModelBudget(context, Settings(_env_file=None, context_input_tokens=2048)).filter(
            data(context)
        )


def test_budget_keeps_all_evidence_when_it_fits():
    items = []
    for i, name in enumerate(["retrieve_context", "send_records_request", "read_evidence"]):
        items.extend(
            [
                {"type": "function_call", "name": name, "call_id": str(i), "arguments": "{}"},
                {"type": "function_call_output", "call_id": str(i), "output": "evidence " * 200},
            ]
        )
    budget = ModelBudget({}, Settings(_env_file=None))
    result = budget.filter(data({}, items))
    outputs = [i for i in result.input if i.get("type") == "function_call_output"]
    assert outputs[0]["output"] == items[1]["output"]
    assert budget.compacted == 0
    assert outputs[1]["output"] == items[3]["output"]
    assert outputs[2]["output"] == items[5]["output"]
    assert len(result.input) == 7


@pytest.mark.parametrize("old_name", ["retrieve_context", "read_evidence"])
def test_pressure_shortens_only_necessary_old_evidence_and_keeps_references(old_name):
    source = {
        "source_id": "older-source",
        "source_hash": "a" * 64,
        "recorded_at": "2026-03-12T00:00:00Z",
        "event_ids": ["old-event"],
        "text": "old evidence " * 4000,
    }
    old = (
        {"results": [source]}
        if old_name == "retrieve_context"
        else {**source, "offset": 180, "next_offset": 9000}
    )
    payloads = [
        old,
        {"results": [{"source_id": "second", "text": "keep second"}]},
        {"source_id": "latest", "text": "keep latest"},
        {"sent": True},
    ]
    names = [old_name, "retrieve_context", "read_evidence", "send_records_request"]
    items = []
    for i, (name, payload) in enumerate(zip(names, payloads)):
        items.extend(
            [
                {"type": "function_call", "name": name, "call_id": str(i), "arguments": "{}"},
                {"type": "function_call_output", "call_id": str(i), "output": json.dumps(payload)},
            ]
        )
    context = {"task": {"objective": "Compare sources"}, "memory": {"hold": "Do not contact"}}
    budget = ModelBudget(context, Settings(_env_file=None, context_input_tokens=3000))
    result = budget.filter(data(context, items))
    outputs = [i for i in result.input if i.get("type") == "function_call_output"]
    shortened = json.loads(outputs[0]["output"])
    reference = shortened["results"][0] if old_name == "retrieve_context" else shortened
    for field in ("source_id", "source_hash", "recorded_at", "event_ids"):
        assert reference[field] == source[field]
    assert "text" not in reference
    assert shortened["truncated"] and shortened["expand_with"] == "read_evidence"
    if old_name == "read_evidence":
        assert shortened["offset"] == 180 and shortened["next_offset"] == 9000
    assert [o["output"] for o in outputs[1:]] == [json.dumps(p) for p in payloads[1:]]
    assert budget.compacted == 1 and budget.calls[-1] <= 3000
    assert json.loads(result.input[0]["content"])["memory"] == context["memory"]
    assert items[1]["output"] == json.dumps(old)  # Saved evidence is unchanged.
    assert len(result.input) == 9  # All function call/output pairs survive.


def test_latest_evidence_that_cannot_fit_requires_review():
    items = [
        {"type": "function_call", "name": "read_evidence", "call_id": "1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "1", "output": "large " * 18000},
    ]
    with pytest.raises(ContextBudgetExceeded):
        ModelBudget({}, Settings(_env_file=None)).filter(data({}, items))


def test_budget_counts_tool_schemas_and_growing_domain_history():
    value = data({})
    value.agent.tools = [
        SimpleNamespace(name="tool", description="word " * 5000, params_json_schema={})
    ]
    with pytest.raises(ContextBudgetExceeded, match="schemas"):
        ModelBudget({}, Settings(_env_file=None, context_input_tokens=2048)).filter(value)
    huge = [{"type": "function_call_output", "call_id": "1", "output": "big " * 18000}]
    with pytest.raises(ContextBudgetExceeded):
        ModelBudget({}, Settings(_env_file=None)).filter(data({}, huge))


def test_retrieval_dates_require_timezone_order_and_unknown_scope_is_forbidden():
    for extra in [
        {"case_id": "invented"},
        {"recorded_after": "2026-03-01T00:00:00"},
        {"recorded_after": "2026-04-01T00:00:00Z", "recorded_before": "2026-03-01T00:00:00Z"},
    ]:
        with pytest.raises(ValidationError):
            RetrievalRequest(question="Where is the MRI?", **extra)


def test_boundary_packing_rechecks_the_wrapped_request_before_failing():
    # UUID-heavy history has significant JSON message escaping overhead. The old
    # single pass accepted the object size then rejected its larger message envelope.
    from uuid import UUID

    from pydantic import TypeAdapter

    from agents_platform.context import dumps, pack_context, tokens

    context = {
        "task": {"id": "task", "objective": "Collect outstanding notes"},
        "memory": {"operational_items": [{"kind": "OBLIGATION", "text": "Keep this rule"}]},
        "history": {
            "retrieval": {
                "results": [
                    {
                        "source_id": str(UUID(int=i + 1)),
                        "text": 'The note says "received" and "pending". ' * 5,
                        "metadata": {"status": "WAITING", "record_type": "RECORDS"},
                    }
                    for i in range(90)
                ]
            }
        },
    }
    value = data(context)
    reserve = (
        tokens(value.agent.instructions)
        + tokens({"tools": [], "output": TypeAdapter(value.agent.output_type).json_schema()})
        + 1024
    )
    limit = reserve + tokens(pack_context(context, 100000)) + 600
    settings = Settings(_env_file=None, context_input_tokens=limit)
    original = json.loads(json.dumps(context))
    initial = pack_context(context, limit - reserve - 64 - 1 - 512)
    old_estimate = reserve + tokens([{"role": "user", "content": dumps(initial)}]) + 64
    assert old_estimate > limit  # The former single-pass filter rejected this input.
    budget = ModelBudget(context, settings)
    result = budget.filter(value)
    packed = json.loads(result.input[0]["content"])
    assert packed["memory"] == original["memory"]
    assert packed["task"] == original["task"]
    assert len(packed["history"]["retrieval"]["results"]) < 90
    assert reserve + tokens(result.input) + 64 <= limit
    assert context == original
    assert budget.report()["failure"] is None
    assert dumps(packed)


def test_unfit_mandatory_context_reports_sizes_without_dumping_values():
    context = {"task": {"objective": "Private required information " * 5000}}
    budget = ModelBudget(context, Settings(_env_file=None))
    with pytest.raises(ContextBudgetExceeded):
        budget.filter(data(context))
    failure = budget.report()["failure"]
    assert failure["reason_code"] == "ESSENTIAL_INPUT_EXCEEDS_BUDGET"
    assert failure["over_budget_tokens"] > 0
    assert failure["snapshot_section_tokens"]["task"] > 12000
    assert "Private" not in json.dumps(failure)

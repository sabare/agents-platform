"""Generate typed specialist delegation tools from a saved capability catalog."""

import json

from agents import FunctionTool
from agents.strict_schema import ensure_strict_json_schema
from pydantic import Field, create_model

from agents_platform.capabilities import DelegationScope, delegate_task
from agents_platform.observability import observed_tool
from agents_platform.task_contracts import Contract


def delegation_tools(catalog, cards, proposals, action_type):
    tools = []
    for card in cards:
        capability = catalog.resolve(card["agent_key"], card["version"])
        arguments_type = create_model(
            f"Delegate_{capability.key}",
            __base__=Contract,
            objective=(str, Field(min_length=1, max_length=2000)),
            assignment=(capability.assignment_type, ...),
        )

        def bind(card, arguments_type):
            async def invoke(context, raw_arguments):
                try:
                    arguments = arguments_type.model_validate_json(raw_arguments)
                    if len(proposals) >= 3:
                        return json.dumps({"error": "At most three delegation proposals per turn"})
                    action = action_type(
                        type="delegate_task",
                        target_index=None,
                        question=None,
                        agent_key=card["agent_key"],
                        agent_version=card["version"],
                        task_title=arguments.objective[:160],
                        assignment_json=arguments.assignment.model_dump_json(),
                        task_objective=arguments.objective,
                    )
                    if action not in proposals:
                        proposals.append(action)
                    return json.dumps(
                        {
                            "status": "PROPOSED",
                            "agent_key": card["agent_key"],
                            "message": "Queued for validation; no task or contact created yet.",
                        }
                    )
                except (ValueError, TypeError) as exc:
                    return json.dumps({"error": str(exc)})

            return invoke

        tools.append(
            FunctionTool(
                name=f"delegate_{card['agent_key']}",
                description=card["description"],
                params_json_schema=ensure_strict_json_schema(arguments_type.model_json_schema()),
                on_invoke_tool=observed_tool(
                    f"delegate_{card['agent_key']}", bind(card, arguments_type)
                ),
            )
        )
    return tools


def validate_delegation(catalog, cards, action, approved_assignments):
    """Validate the entire batch before any task creation, including direct model output."""
    card = next(
        (
            c
            for c in cards
            if c["agent_key"] == action.agent_key and c["version"] == action.agent_version
        ),
        None,
    )
    if card is None:
        raise ValueError("Delegation refers to an unavailable capability/version")
    capability = catalog.resolve(action.agent_key, action.agent_version)
    if capability.card() != card:
        raise ValueError("Capability registration changed during planning")
    assignment = capability.assignment_type.model_validate_json(action.assignment_json or "{}")
    payload = assignment.model_dump(mode="json")
    if approved_assignments and not any(
        a["agent_key"] == action.agent_key
        and a["agent_version"] == action.agent_version
        and a["assignment"] == payload
        and a["objective"] == action.task_objective
        for a in approved_assignments
    ):
        raise ValueError("Delegation differs from the operator-approved assignment")
    if not action.task_objective:
        raise ValueError("Delegation requires a focused task objective")
    return card, payload


async def apply_delegation(session, catalog, coord, action, card, assignment):
    scope = DelegationScope(
        tenant_id=coord.tenant_id,
        case_id=coord.case_id,
        parent_task_id=coord.group_task_id,
        agent_version=card["version"],
        objective=action.task_objective,
        actor_ref=f"agent/case_supervisor/{coord.agent_version}",
    )
    return await delegate_task(session, catalog, card, scope, assignment)


def supervisor_delegation_cards(context):
    """Offer only callable choices; legacy target actions are not generic tool calls."""
    cards = context.get("capabilities", [])
    approved = context.get("objective", {}).get("approved_assignments", [])
    if approved:
        allowed = {(a["agent_key"], a["agent_version"]) for a in approved}
        return [card for card in cards if (card["agent_key"], card["version"]) in allowed]
    if context.get("objective", {}).get("targets"):
        return []
    return cards

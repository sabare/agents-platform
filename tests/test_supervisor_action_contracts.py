"""The model may only choose delegation actions allowed by the approved objective."""

import pytest
from pydantic import ValidationError

from agents_platform.coordination import (
    SupervisorDecision,
    TargetSupervisorDecision,
    supervisor_decision_type,
)


def proposal(**action):
    return dict(
        summary="Collect the approved records",
        evidence_event_ids=[],
        objective_satisfied=False,
        next_review_at=None,
        actions=[{"type": "create_task", "target_index": 0, "question": None, **action}],
    )


def test_fixed_targets_constrain_the_actual_model_response_contract():
    contract = supervisor_decision_type({"objective": {"targets": [{"provider": "approved"}]}})
    assert contract is TargetSupervisorDecision
    assert contract.model_validate(proposal()).actions[0].type == "create_task"
    with pytest.raises(ValidationError):
        contract.model_validate(
            proposal(type="delegate_task", agent_key="medical_records_collector")
        )
    with pytest.raises(ValidationError):
        contract.model_validate(proposal(agent_key="medical_records_collector"))


def test_registered_generic_delegation_is_still_available():
    for objective in ({}, {"approved_assignments": [{"agent_key": "new_agent"}]}):
        assert supervisor_decision_type({"objective": objective}) is SupervisorDecision
    assert (
        SupervisorDecision.model_validate(proposal(type="delegate_task")).actions[0].type
        == "delegate_task"
    )

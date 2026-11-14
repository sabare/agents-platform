"""A ready-looking partial plan cannot silently drop part of the operator request."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from agents import Runner, Usage

from agents_platform.assignment_router import AssignmentPlan, PlanCoverage, interpret_description
from agents_platform.config import Settings


def proposed():
    return AssignmentPlan(
        status="ready",
        title="Collect hospital bills",
        summary="Collect bills; external email delivery requires manual handling.",
        steps=[
            {
                "kind": "records",
                "targets": [
                    {
                        "patient_contact_id": str(UUID(int=1)),
                        "provider_contact_id": str(UUID(int=2)),
                        "requirements": [{"record_type": "BILLS", "all_dates": True}],
                    }
                ],
            }
        ],
    )


@pytest.mark.parametrize("covered", [False, True])
async def test_independent_coverage_review_gates_ready_plan_and_accounts_for_both_calls(
    monkeypatch, covered
):
    responses = [
        proposed(),
        PlanCoverage(
            fully_covered=covered,
            gaps=[] if covered else ["Actual external PDF delivery is not supported."],
            questions=[]
            if covered
            else ["Should we collect bills only, with delivery handled separately?"],
        ),
    ]
    calls = []

    async def run(agent, input, **kwargs):
        calls.append(agent.name)
        return SimpleNamespace(
            final_output=responses[len(calls) - 1],
            context_wrapper=SimpleNamespace(
                usage=Usage(requests=1, input_tokens=10, output_tokens=5)
            ),
        )

    monkeypatch.setattr(Runner, "run", run)
    plan, usage = await interpret_description(
        Settings(_env_file=None, agent_mode="openai"),
        {
            "description": "Collect bills and email the actual PDFs to outside@example.com.",
            "contacts": [],
        },
    )
    assert len(calls) == 2 and usage["requests"] == 2 and usage["input_tokens"] == 20
    if covered:
        assert plan.status == "ready" and plan.steps
    else:
        assert plan.status == "needs_clarification" and not plan.steps and plan.questions
        assert usage["unapproved_proposal"]["status"] == "ready"

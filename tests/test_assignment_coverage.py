"""A ready-looking partial plan cannot silently drop part of the operator request."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from agents import Runner, Usage

from agents_platform.assignment_router import AssignmentPlan, PlanCoverage, interpret_description
from agents_platform.config import Settings
from agents_platform.task_contracts import RecordRequirementInput


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


def test_checkin_coverage_uses_resolved_business_fields_instead_of_agent_contracts():
    from datetime import datetime, timezone

    from agents_platform.assignment_router import coverage_packet

    plan = AssignmentPlan(
        status="ready",
        summary="Check in",
        steps=[
            {
                "kind": "check_in",
                "assignment": {
                    "case_id": str(UUID(int=3)),
                    "client_contact_id": str(UUID(int=1)),
                    "scheduled_at": datetime.now(timezone.utc).isoformat(),
                    "objective": "Check in with Jordan",
                },
            }
        ],
    )
    packet = coverage_packet(
        {
            "description": "Check in with Jordan in two minutes",
            "contacts": [{"id": str(UUID(int=1)), "name": "Jordan"}],
            "capabilities": [{"assignment_schema": {"required": ["message"]}}],
        },
        plan,
    )
    work = packet["validated_work"][0]
    assert work["message_to_client"] and work["client"] == "Jordan"
    assert work["task_title"] == "Check in with Jordan" and work["recurring"] is False
    assert "capabilities" not in packet and "assignment_schema" not in str(packet)
    assert packet["technical_schema_validation"] == "PASSED"


def test_records_coverage_includes_automatic_reporting_without_extra_assignment():
    from agents_platform.assignment_router import coverage_packet

    plan = proposed()
    plan.steps[0].targets[0].requirements = [
        RecordRequirementInput(record_type=kind, all_dates=True)
        for kind in ["RECORDS", "SURGICAL_NOTES"]
    ]
    packet = coverage_packet(
        {
            "description": "collect all general medical records and surgical note from Jordan "
            "Lee from Example Hospital, for all dates. tell me about mising items or anything "
            "that needs my help",
            "contacts": [
                {"id": str(UUID(int=1)), "name": "Jordan Lee"},
                {"id": str(UUID(int=2)), "name": "Example Hospital"},
            ],
        },
        plan,
    )
    reporting = packet["included_platform_behavior"]
    assert reporting["separate_reporting_assignment_required"] is False
    assert reporting["external_delivery_included"] is False
    assert "Missing or outstanding items" in reporting["automatic_updates"]
    assert "Blockers and items needing teammate help" in reporting["automatic_updates"]
    assert "Activity" in reporting["reporting_destination"]
    assert len(packet["validated_work"]) == 1
    assert packet["validated_work"][0]["provider"] == "Example Hospital"
    assert "report missing items" in packet["validated_work"][0]["work"]

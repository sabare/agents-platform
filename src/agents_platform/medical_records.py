"""Medical-records business adapter and the original analysis-only agent factory."""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from agents import Agent, Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import Field
from sqlalchemy import select

from agents_platform.domains import DomainSnapshot, DomainValidation, ToolResult, ToolSpec
from agents_platform.models import Contact, RecordEvidence, RecordRequirement, RecordsRequest
from agents_platform.registry import load_definition
from agents_platform.schemas import SpecialistResult
from agents_platform.task_contracts import Contract, NextAction, Observation, RecordsDecision
from agents_platform.tasks import read_task


def create_medical_records_agent(model: str) -> Agent:
    return Agent(
        name="Medical Records Agent",
        model=model,
        instructions=load_definition("medical_records_collector", 1)["instructions"],
        output_type=SpecialistResult,
    )


class RecordsInput(Contract):
    request_id: UUID


class ReminderArguments(Contract):
    occurrence: int = Field(ge=1, le=3)


class MedicalRecordsAdapter:
    key = "medical_records"
    version = 1
    task_type = "medical_records.collect"
    input_schema = "records_assignment.v1"
    agent_key = "medical_records_collector"
    output_schemas = ("records_decision.v1", "specialist_result.v1")
    decision_type = RecordsDecision
    tools = (
        ToolSpec(
            "send_records_reminder",
            "Send the next due simulated reminder; maximum three per task.",
            "contact",
            ReminderArguments,
            task_key=lambda args: f"records-reminder/{args.occurrence}",
        ),
        ToolSpec("get_provider_details", "Look up this task's simulated provider.", "read"),
        ToolSpec(
            "send_records_request",
            "Send the simulated initial request once.",
            "contact",
            task_key="initial-send",
        ),
        ToolSpec(
            "check_request_status", "Read provider status and saved document source IDs.", "read"
        ),
    )

    async def load(self, session, task):
        assignment = RecordsInput.model_validate(task.input_json)
        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == task.tenant_id,
                RecordsRequest.case_id == task.case_id,
                RecordsRequest.task_id == task.id,
                RecordsRequest.id == assignment.request_id,
            )
        )
        if request is None:
            raise ValueError("Records assignment has no matching request")
        view = (await read_task(session, task.tenant_id, task.id)).model_dump(mode="json")
        reminder = {
            "enabled": task.agent_version >= 3,
            "count": request.reminder_count,
            "maximum": request.max_reminders,
            "interval_seconds": request.reminder_interval_seconds,
            "next_occurrence": request.reminder_count + 1,
            "next_due_at": request.next_reminder_at.isoformat()
            if request.next_reminder_at
            else None,
            "is_due": bool(
                task.agent_version >= 3
                and request.next_reminder_at
                and request.next_reminder_at <= datetime.now(timezone.utc)
                and request.status in ("SENT", "PENDING")
                and request.reminder_count < request.max_reminders
            ),
        }
        return DomainSnapshot(
            state={"records_request": view["records_request"], "reminder": reminder},
            instruction_provider_ids=(request.provider_contact_id,),
            task_fields={"records_request": view["records_request"]},
            action_requirements={
                "request_state": request.status,
                "before_waiting": "If DRAFT, send the initial request once before waiting. "
                "A prior resolved hold does not forbid contact. Open holds still block it.",
                "last_validation_feedback": task.progress_json.get("validation_feedback"),
            },
        )

    async def execute_tool(self, session, task, name, arguments):
        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == task.tenant_id,
                RecordsRequest.case_id == task.case_id,
                RecordsRequest.task_id == task.id,
            )
        )
        if request is None:
            raise ValueError("Records request missing")
        evidence_ids = ()
        if name == "get_provider_details":
            provider = await session.scalar(
                select(Contact).where(
                    Contact.tenant_id == task.tenant_id, Contact.id == request.provider_contact_id
                )
            )
            data = {"provider": provider.display_name, "simulated": True}
        elif name == "send_records_request":
            if request.status == "DRAFT":
                request.status = "SENT"
                if task.agent_version >= 3:
                    request.next_reminder_at = datetime.now(timezone.utc) + timedelta(
                        seconds=request.reminder_interval_seconds
                    )
            data = {"request_ref": f"SIM-{request.id}", "simulated": True}
        elif name == "send_records_reminder":
            if task.agent_version < 3:
                raise ValueError("This pinned agent version cannot send reminders")
            if request.status not in ("SENT", "PENDING"):
                raise ValueError("Reminders require an outstanding sent request")
            if (
                arguments.occurrence != request.reminder_count + 1
                or request.reminder_count >= request.max_reminders
            ):
                raise ValueError("Reminder occurrence is not the next permitted occurrence")
            if request.next_reminder_at is None or request.next_reminder_at > datetime.now(
                timezone.utc
            ):
                raise ValueError("Reminder is not due")
            request.reminder_count += 1
            request.next_reminder_at = (
                datetime.now(timezone.utc) + timedelta(seconds=request.reminder_interval_seconds)
                if request.reminder_count < request.max_reminders
                else None
            )
            return ToolResult(
                {
                    "occurrence": arguments.occurrence,
                    "request_ref": f"SIM-{request.id}",
                    "summary": f"Simulated provider reminder {arguments.occurrence} sent.",
                    "next_due_at": request.next_reminder_at.isoformat()
                    if request.next_reminder_at
                    else None,
                    "simulated": True,
                },
                source_kind="SIMULATED_RECORDS_REMINDER",
                event_type="records.reminder_sent",
            )
        elif name == "check_request_status":
            evidence_ids = tuple(
                (
                    await session.scalars(
                        select(RecordEvidence.source_id)
                        .join(
                            RecordRequirement, RecordRequirement.id == RecordEvidence.requirement_id
                        )
                        .where(
                            RecordEvidence.tenant_id == task.tenant_id,
                            RecordEvidence.case_id == task.case_id,
                            RecordRequirement.tenant_id == task.tenant_id,
                            RecordRequirement.request_id == request.id,
                        )
                    )
                ).all()
            )
            data = {
                "status": request.status,
                "received_source_ids": [str(i) for i in evidence_ids],
                "simulated": True,
            }
        else:
            raise ValueError("Unsupported records tool")
        return ToolResult(data, evidence_ids, "SIMULATED_TOOL_RESULT")

    def review(self, reason):
        return RecordsDecision(
            summary=reason,
            observations=[],
            received_source_ids=[],
            next_action=NextAction(type="needs_human", follow_up_seconds=None, reason=reason),
        )

    def scripted_model(self, context, follow_up_seconds):
        return ScriptedModel(context, follow_up_seconds)

    async def validate(self, session, task, context, decision, results, action):
        documents = {x for r in results for x in r.get("received_source_ids", [])}
        requested = {str(i) for i in decision.received_source_ids}
        if not requested.issubset(documents):
            raise ValueError("Agent invented received document evidence")
        request_id = UUID(context["domain"]["state"]["records_request"]["id"])
        requirements = (
            await session.scalars(
                select(RecordRequirement).where(
                    RecordRequirement.tenant_id == task.tenant_id,
                    RecordRequirement.request_id == request_id,
                    RecordRequirement.required.is_(True),
                )
            )
        ).all()
        evidence = (
            await session.execute(
                select(RecordEvidence.requirement_id, RecordEvidence.source_id).where(
                    RecordEvidence.tenant_id == task.tenant_id,
                    RecordEvidence.case_id == task.case_id,
                    RecordEvidence.requirement_id.in_([r.id for r in requirements]),
                )
            )
        ).all()
        covered = {r for r, source in evidence if str(source) in requested}
        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == task.tenant_id, RecordsRequest.task_id == task.id
            )
        )
        if action.type == "complete" and (
            request.status != "RECEIVED" or covered != {r.id for r in requirements}
        ):
            raise ValueError("Completion requires saved evidence for every required item")
        if (
            task.agent_version >= 3
            and action.type == "wait"
            and request.status != "RECEIVED"
            and request.reminder_count >= request.max_reminders
        ):
            action = self.review(
                "Simulated reminder limit reached; "
                "teammate review or a provider update is required."
            ).next_action
        due_reminder = bool(
            task.agent_version >= 3
            and action.type == "wait"
            and request.status in ("SENT", "PENDING")
            and request.next_reminder_at
            and request.next_reminder_at <= datetime.now(timezone.utc)
            and request.reminder_count < request.max_reminders
        )
        invalid_draft = action.type == "wait" and request.status == "DRAFT"
        invalid = invalid_draft or due_reminder
        replans = task.progress_json.get("reminder_replans" if due_reminder else "draft_replans", 0)
        feedback = None
        if invalid:
            feedback = (
                f"Wait rejected: simulated reminder {request.reminder_count + 1} is due. "
                "Send the next permitted reminder or request human review."
                if due_reminder
                else (
                    "Wait rejected: request is still DRAFT. "
                    "Send it once or explain why human review is needed."
                )
            )
            if replans >= 2:
                action = self.review(
                    "Agent repeatedly waited without a due reminder. Review the follow-up."
                    if due_reminder
                    else "Agent repeatedly tried to wait on an unsent request. Review the draft."
                ).next_action
        return DomainValidation(
            action=action,
            rejected=invalid,
            replan_reason=feedback if invalid and replans < 2 else None,
            progress={
                "draft_replans": replans + 1 if invalid_draft else 0,
                "reminder_replans": replans + 1 if due_reminder else 0,
                "validation_feedback": feedback,
                "outstanding_requirement_ids": [
                    str(r.id) for r in requirements if r.id not in covered
                ],
            },
        )


class ScriptedModel(Model):
    """Deterministic SDK model for offline demos; never presented as an LLM call."""

    def __init__(self, context, follow_up_seconds):
        self.follow_up_seconds = follow_up_seconds
        self.plan = ["get_provider_details"]
        if context["task"]["records_request"]["status"] == "DRAFT":
            self.plan.append("send_records_request")
        reminder = context.get("domain", {}).get("state", {}).get("reminder", {})
        self.reminder_occurrence = reminder.get("next_occurrence", 1)
        if reminder.get("enabled") and reminder.get("is_due"):
            self.plan.append("send_records_reminder")
        self.plan.append("check_request_status")
        self.turn = 0

    async def get_response(self, system_instructions, input, *args, **kwargs):
        if self.turn < len(self.plan):
            name = self.plan[self.turn]
            self.turn += 1
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    name=name,
                    arguments=json.dumps({"occurrence": self.reminder_occurrence})
                    if name == "send_records_reminder"
                    else "{}",
                    call_id=f"demo-call-{self.turn}",
                    id=f"demo-call-{self.turn}",
                )
            ]
        else:
            outputs = [item for item in input if item.get("type") == "function_call_output"]
            state = json.loads(outputs[-1]["output"])
            complete = state["status"] == "RECEIVED"
            decision = RecordsDecision(
                summary="All required simulated records received."
                if complete
                else "Provider processing is pending.",
                observations=[
                    Observation(
                        statement="Simulated provider status: " + state["status"],
                        source_id=state["source_id"],
                    )
                ],
                received_source_ids=state["received_source_ids"],
                next_action=NextAction(
                    type="complete" if complete else "wait",
                    follow_up_seconds=None if complete else self.follow_up_seconds,
                    reason="Saved simulated records are available."
                    if complete
                    else "Wait for provider response.",
                ),
            )
            output = [
                ResponseOutputMessage(
                    type="message",
                    id="demo-final",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=decision.model_dump_json(), annotations=[]
                        )
                    ],
                )
            ]
        return ModelResponse(output=output, usage=Usage(requests=1), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Demo uses Runner.run, not streaming")
        yield  # pragma: no cover

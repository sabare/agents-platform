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

from agents_platform.domains import (
    ActionNotDue,
    ActionStateChanged,
    DecisionValidationError,
    DomainSnapshot,
    DomainValidation,
    ToolResult,
    ToolSpec,
)
from agents_platform.models import (
    Case,
    CaseContact,
    Contact,
    RecordEvidence,
    RecordRequirement,
    RecordsRequest,
)
from agents_platform.record_receipts import (
    follow_up_content,
    missing_requirements,
    requirement_coverage,
)
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


async def contact_scope_reason(session, task, request):
    case = await session.scalar(
        select(Case).where(
            Case.tenant_id == task.tenant_id,
            Case.id == task.case_id,
        )
    )
    if not case or case.status != "OPEN":
        return "The case is closed; do not contact the provider."
    for contact_id, roles in (
        (request.patient_contact_id, {"CLIENT", "PATIENT"}),
        (request.provider_contact_id, {"MEDICAL_PROVIDER"}),
    ):
        participant = await session.scalar(
            select(CaseContact).where(
                CaseContact.tenant_id == task.tenant_id,
                CaseContact.case_id == task.case_id,
                CaseContact.contact_id == contact_id,
            )
        )
        if not participant or not roles.intersection(participant.roles):
            return (
                "Records participant roles changed; review the patient and provider before contact."
            )
    return None


class MedicalRecordsAdapter:
    async def on_communication_commitment(self, session, task, due_at, source_id):
        request = await session.scalar(
            select(RecordsRequest).where(
                RecordsRequest.tenant_id == task.tenant_id, RecordsRequest.task_id == task.id
            )
        )
        if request and request.status != "RECEIVED":
            request.next_reminder_at = max(
                [due_at] + [d for d in (request.next_reminder_at, request.callback_at) if d]
            )

    def communication_review_contract(self, analysis):
        from agents_platform.communications import (
            ResolveAuthorizedCommunication,
            ResolveCommunication,
        )

        return (
            ResolveAuthorizedCommunication
            if analysis.content.get("requires_authorization")
            else ResolveCommunication
        )

    async def validate_communication_resolution(self, session, task, analysis, payload):
        if not analysis.content.get("requires_authorization"):
            return
        from agents_platform.models import Source
        from agents_platform.tasks import TaskError

        document = await session.scalar(
            select(Source).where(
                Source.tenant_id == task.tenant_id,
                Source.case_id == task.case_id,
                Source.id == payload.authorization_source_id,
                Source.kind == "UPDATED_RECORDS_AUTHORIZATION",
                Source.content["task_id"].astext == str(task.id),
            )
        )
        if not document:
            raise TaskError(422, "Supply a reviewed updated authorization document for this task")
        task.progress_json = {
            **task.progress_json,
            "communication_authorization_source_id": str(document.id),
        }

    async def ingest_updates(self, database, settings, tenant_id, task_id):
        from agents_platform.provider_responses import ingest_pending

        await ingest_pending(database, settings, tenant_id, task_id)

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

    async def advance_human_work(self, session, task):
        from agents_platform.human_request_handlers import advance_records_intervention

        return await advance_records_intervention(session, task)

    async def request_human_help(self, session, task, reason):
        from agents_platform.human_request_handlers import ensure_records_request
        from agents_platform.provider_responses import ensure_review

        return await ensure_review(session, task) or await ensure_records_request(
            session, task, reason
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
        scope_reason = await contact_scope_reason(session, task, request)
        from agents_platform.provider_responses import action_blocker

        provider_blocker = await action_blocker(session, task)
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
                not provider_blocker
                and task.agent_version >= 3
                and request.next_reminder_at
                and request.next_reminder_at <= datetime.now(timezone.utc)
                and request.status in ("SENT", "PENDING", "PARTIAL")
                and request.reminder_count < request.max_reminders
            ),
        }
        return DomainSnapshot(
            state={
                "records_request": view["records_request"],
                "reminder": reminder,
                "provider_blocker": provider_blocker,
            },
            evidence_source_ids=tuple(
                UUID(source_id)
                for requirement in view["records_request"]["requirements"]
                for source_id in requirement["evidence_source_ids"]
            ),
            ready_at=(request.callback_at or request.next_reminder_at)
            if request.status != "RECEIVED" and not scope_reason and not provider_blocker
            else None,
            instruction_provider_ids=(request.provider_contact_id,),
            task_fields={"records_request": view["records_request"]},
            action_requirements={
                "suspension_reason": scope_reason,
                "request_state": request.status,
                "provider_blocker": provider_blocker,
                "authorization_policy": (
                    "An open provider blocker requires human review. Do not send, wait past it, "
                    "or complete. Approval does not prove missing records arrived."
                ),
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
        if name in ("send_records_request", "send_records_reminder"):
            reason = await contact_scope_reason(session, task, request)
            from agents_platform.provider_responses import action_blocker

            reason = reason or await action_blocker(session, task)
            if reason:
                raise ActionStateChanged(reason)
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
            data = {
                "request_ref": f"SIM-{request.id}",
                "simulated": True,
                "authorization_source_id": task.progress_json.get(
                    "communication_authorization_source_id"
                ),
                **follow_up_content(await missing_requirements(session, task, request)),
            }
        elif name == "send_records_reminder":
            if task.agent_version < 3:
                raise ValueError("This pinned agent version cannot send reminders")
            if request.status not in ("SENT", "PENDING", "PARTIAL"):
                raise ValueError("Reminders require an outstanding sent request")
            if (
                arguments.occurrence != request.reminder_count + 1
                or request.reminder_count >= request.max_reminders
            ):
                raise ValueError("Reminder occurrence is not the next permitted occurrence")
            if request.next_reminder_at is None:
                raise ValueError("Reminder is not due")
            if request.next_reminder_at > datetime.now(timezone.utc):
                raise ActionNotDue(request.next_reminder_at)
            missing = await missing_requirements(session, task, request)
            if not missing:
                raise ValueError("No required items remain outstanding; do not send a reminder")
            callback_at, callback_timezone = request.callback_at, request.callback_timezone
            if callback_at and callback_at > datetime.now(timezone.utc):
                raise ActionNotDue(callback_at)
            request.callback_at = request.callback_timezone = None
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
                    "summary": f"Simulated provider reminder {arguments.occurrence} sent for "
                    + ", ".join(r["record_type"].lower() for r in missing)
                    + ".",
                    **follow_up_content(missing),
                    "callback_at": callback_at.isoformat() if callback_at else None,
                    "callback_timezone": callback_timezone,
                    "next_due_at": request.next_reminder_at.isoformat()
                    if request.next_reminder_at
                    else None,
                    "simulated": True,
                    "authorization_source_id": task.progress_json.get(
                        "communication_authorization_source_id"
                    ),
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
            coverage = await requirement_coverage(session, task.tenant_id, request.id)
            data = {
                "requirements": coverage,
                "outstanding_requirement_ids": [
                    r["id"] for r in coverage if r["required"] and r["status"] == "OUTSTANDING"
                ],
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
        from agents_platform.provider_responses import action_blocker

        blocker = await action_blocker(session, task)
        if blocker:
            if action.type == "complete":
                raise DecisionValidationError("Completion blocked: " + blocker)
            return DomainValidation(action=self.review(blocker).next_action)
        documents = {x for r in results for x in r.get("received_source_ids", [])}
        documents.update(context.get("domain", {}).get("evidence_source_ids", []))
        requested = {str(i) for i in decision.received_source_ids}
        if not requested.issubset(documents):
            raise DecisionValidationError(
                "Agent invented received document evidence. received_source_ids must contain only "
                "document evidence IDs from the domain snapshot or check_request_status. "
                f"Currently available document evidence IDs: {sorted(documents)}. "
                "A sent request or reminder is not a received document."
            )
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
            raise DecisionValidationError(
                "Completion requires saved evidence for every required item"
            )
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
            and request.status in ("SENT", "PENDING", "PARTIAL")
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
                    str(r.id)
                    for r in requirements
                    if r.id not in {requirement_id for requirement_id, _ in evidence}
                ],
            },
        )


class ScriptedModel(Model):
    """Deterministic SDK model for offline demos; never presented as an LLM call."""

    def __init__(self, context, follow_up_seconds):
        self.follow_up_seconds = follow_up_seconds
        self.blocker = context.get("domain", {}).get("state", {}).get("provider_blocker")
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
                else "Outstanding items: "
                + ", ".join(
                    r["record_type"].lower()
                    for r in state["requirements"]
                    if r["required"] and r["status"] == "OUTSTANDING"
                )
                + ".",
                observations=[
                    Observation(
                        statement="Simulated provider status: " + state["status"],
                        source_id=state["source_id"],
                    )
                ],
                received_source_ids=state["received_source_ids"],
                next_action=NextAction(
                    type="needs_human" if self.blocker else "complete" if complete else "wait",
                    follow_up_seconds=None if complete or self.blocker else self.follow_up_seconds,
                    reason=self.blocker
                    or (
                        "Saved simulated records are available."
                        if complete
                        else "Wait for provider response."
                    ),
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

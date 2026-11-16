from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.db import session_dependency
from agents_platform.models import ActivityEvent, Execution, ToolInvocation
from agents_platform.task_contracts import CreateRecordsTask, EventView, ProviderUpdate, TaskView
from agents_platform.tasks import TaskError, create_records_task, read_task, receive_provider_update

bearer = HTTPBearer(auto_error=False)


async def require_operator(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    """Local operator credential grants all-firm access; not firm-user authentication."""
    from agents_platform.audit import bind_audit, make_event, persist

    expected = request.app.state.settings.operator_api_key.get_secret_value()
    allowed = bool(credentials) and compare_digest(
        credentials.credentials.encode(), expected.encode()
    )
    actor = "local-operator" if allowed else "anonymous"
    request.state.audit_actor = actor
    identity = {}
    for name in ("tenant_id", "case_id", "task_id"):
        try:
            identity[name] = UUID(str(request.path_params[name]))
        except (KeyError, ValueError):
            pass
    bind_audit(actor_ref=actor, **identity)
    route = getattr(request.scope.get("route"), "path", "unmatched")
    await persist(
        request.app.state.database,
        make_event(
            event_type="authorization.decision",
            action=f"{request.method} {route}",
            actor_ref=actor,
            outcome="ALLOWED" if allowed else "DENIED",
            reason_code="OPERATOR_KEY_MATCH" if allowed else "INVALID_OPERATOR_CREDENTIAL",
            policy_version="local-operator.v1",
        ),
    )
    if not allowed:
        raise HTTPException(
            401,
            "Invalid operator credential",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(
    prefix="/v1/firms/{tenant_id}/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_operator)],
)
Session = Annotated[AsyncSession, Depends(session_dependency)]


@router.post("/medical-records", response_model=TaskView, status_code=201)
async def create_task(
    tenant_id: UUID,
    body: CreateRecordsTask,
    response: Response,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        task, created = await create_records_task(session, tenant_id, body, idempotency_key.strip())
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.status_code = 201 if created else 200
    response.headers["Location"] = f"/v1/firms/{tenant_id}/tasks/{task.id}"
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return task


@router.get("/{task_id}", response_model=TaskView)
async def get_task(tenant_id: UUID, task_id: UUID, session: Session):
    try:
        return await read_task(session, tenant_id, task_id)
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/{task_id}/events", response_model=list[EventView])
async def task_events(tenant_id: UUID, task_id: UUID, session: Session):
    try:
        await read_task(session, tenant_id, task_id)
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    events = (
        await session.scalars(
            select(ActivityEvent)
            .where(ActivityEvent.tenant_id == tenant_id, ActivityEvent.task_id == task_id)
            .order_by(ActivityEvent.created_at, ActivityEvent.id)
            .limit(100)
        )
    ).all()
    return [
        EventView(**{name: getattr(event, name) for name in EventView.model_fields})
        for event in events
    ]


@router.post("/{task_id}/provider-updates", summary="Simulate a provider response (demo)")
async def provider_update(
    tenant_id: UUID,
    task_id: UUID,
    body: ProviderUpdate,
    session: Session,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        source_id, created = await receive_provider_update(
            session, tenant_id, task_id, body, idempotency_key.strip()
        )
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.status_code = 201 if created else 200
    return {"source_id": str(source_id), "simulated": True, "replayed": not created}


@router.get("/{task_id}/executions")
async def executions(tenant_id: UUID, task_id: UUID, session: Session):
    try:
        await read_task(session, tenant_id, task_id)
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    runs = (
        await session.scalars(
            select(Execution)
            .where(Execution.tenant_id == tenant_id, Execution.task_id == task_id)
            .order_by(Execution.cycle.desc())
            .limit(20)
        )
    ).all()
    result = []
    for run in reversed(runs):
        tools = (
            await session.scalars(
                select(ToolInvocation)
                .where(ToolInvocation.tenant_id == tenant_id, ToolInvocation.execution_id == run.id)
                .order_by(ToolInvocation.created_at)
            )
        ).all()
        result.append(
            {
                "id": str(run.id),
                "cycle": run.cycle,
                "created_at": run.created_at,
                "finished_at": run.finished_at,
                "status": run.status,
                "mode": run.mode,
                "attempt": run.attempt,
                "context": run.context_json,
                "observability": run.context_json.get("observability", {}),
                "decision": run.decision_json,
                "outcome": run.outcome_json,
                "usage": run.usage_json,
                "error_type": run.error_type,
                "tools": [
                    {
                        "name": t.tool_name,
                        "logical_key": t.logical_key,
                        "arguments": t.arguments_json,
                        "result": t.result_json,
                    }
                    for t in tools
                ],
            }
        )
    return result

"""Operator entry points for a durable case objective; normal task APIs remain unchanged."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.capabilities import discover_capabilities
from agents_platform.coordination import CaseObjective, submit_objective
from agents_platform.db import session_dependency
from agents_platform.models import Case, CaseCoordination
from agents_platform.task_api import require_operator
from agents_platform.tasks import TaskError

router = APIRouter(
    prefix="/v1/firms/{tenant_id}/cases/{case_id}/supervisor",
    tags=["supervisor"],
    dependencies=[Depends(require_operator)],
)
Session = Annotated[AsyncSession, Depends(session_dependency)]


def coordination_view(coord):
    return {
        "case_id": str(coord.case_id),
        "group_task_id": str(coord.group_task_id),
        "revision": coord.revision,
        "status": coord.status,
        "objective": coord.objective_json,
        "plan": coord.plan_json,
        "agent_version": coord.agent_version,
        "workflow_id": coord.workflow_id,
    }


@router.get("")
async def get_supervisor(tenant_id: UUID, case_id: UUID, session: Session):
    if not await session.scalar(
        select(Case.id).where(Case.tenant_id == tenant_id, Case.id == case_id)
    ):
        raise HTTPException(404, "Case not found")
    coord = await session.scalar(
        select(CaseCoordination).where(
            CaseCoordination.tenant_id == tenant_id, CaseCoordination.case_id == case_id
        )
    )
    return (
        coordination_view(coord)
        if coord
        else {"case_id": str(case_id), "status": "NOT_CONFIGURED", "revision": None}
    )


@router.post("", status_code=201)
async def set_objective(
    tenant_id: UUID,
    case_id: UUID,
    body: CaseObjective,
    session: Session,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
):
    if not idempotency_key.strip():
        raise HTTPException(422, "Idempotency-Key must not be blank")
    try:
        group_id, created = await submit_objective(
            session, tenant_id, case_id, body, idempotency_key.strip()
        )
    except TaskError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    response.status_code = 201 if created else 200
    return {"group_task_id": str(group_id), "replayed": not created}


@router.get("/capabilities")
async def available_capabilities(tenant_id: UUID, case_id: UUID, session: Session):
    if not await session.scalar(
        select(Case.id).where(Case.tenant_id == tenant_id, Case.id == case_id)
    ):
        raise HTTPException(404, "Case not found")
    return {"capabilities": await discover_capabilities().available(session)}

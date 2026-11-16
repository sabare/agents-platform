"""Read the common journal using the existing all-firm operator credential."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from agents_platform.task_api import Session, require_operator

router = APIRouter(
    prefix="/v1/firms/{tenant_id}/audit", tags=["audit"], dependencies=[Depends(require_operator)]
)


@router.get("/events")
async def events(
    tenant_id: UUID,
    session: Session,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    task_id: UUID | None = None,
    case_id: UUID | None = None,
):
    rows = (
        (
            await session.execute(
                text("""
      SELECT sequence,event,previous_hash,event_hash FROM audit.events
      WHERE stream_id=:tenant AND sequence>:after
        AND (CAST(:task AS text) IS NULL OR event->>'task_id'=:task)
        AND (CAST(:caseid AS text) IS NULL OR event->>'case_id'=:caseid)
      ORDER BY sequence LIMIT :limit
    """),
                {
                    "tenant": tenant_id,
                    "after": after,
                    "limit": limit,
                    "task": str(task_id) if task_id else None,
                    "caseid": str(case_id) if case_id else None,
                },
            )
        )
        .mappings()
        .all()
    )
    return {
        "events": [dict(r) for r in rows],
        "next_after": rows[-1]["sequence"] if rows else after,
    }

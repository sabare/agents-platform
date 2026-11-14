"""Evidence-backed coverage shared by intake, agent tools and operator follow-ups."""

from sqlalchemy import select

from agents_platform.models import RecordEvidence, RecordRequirement


async def requirement_coverage(session, tenant_id, request_id):
    requirements = (
        await session.scalars(
            select(RecordRequirement)
            .where(
                RecordRequirement.tenant_id == tenant_id,
                RecordRequirement.request_id == request_id,
            )
            .order_by(RecordRequirement.record_type, RecordRequirement.id)
        )
    ).all()
    evidence = (
        await session.execute(
            select(
                RecordEvidence.requirement_id,
                RecordEvidence.source_id,
            ).where(
                RecordEvidence.tenant_id == tenant_id,
                RecordEvidence.requirement_id.in_([r.id for r in requirements]),
            )
        )
    ).all()
    sources = {}
    for requirement_id, source_id in evidence:
        sources.setdefault(requirement_id, []).append(str(source_id))
    return [
        {
            "id": str(r.id),
            "record_type": r.record_type,
            "all_dates": r.all_dates,
            "from_date": r.from_date.isoformat() if r.from_date else None,
            "through_date": r.through_date.isoformat() if r.through_date else None,
            "required": r.required,
            "status": "RECEIVED" if r.id in sources else "OUTSTANDING",
            "evidence_source_ids": sorted(sources.get(r.id, [])),
        }
        for r in requirements
    ]


async def missing_requirements(session, task, request):
    return [
        r
        for r in await requirement_coverage(session, task.tenant_id, request.id)
        if r["required"] and r["status"] == "OUTSTANDING"
    ]


def follow_up_content(missing):
    labels = [
        r["record_type"].lower()
        + (" (all dates)" if r["all_dates"] else f" ({r['from_date']} through {r['through_date']})")
        for r in missing
    ]
    return {
        "requested_requirement_ids": [r["id"] for r in missing],
        "requested_items": missing,
        "message": "Please provide the outstanding items: " + "; ".join(labels) + ".",
    }

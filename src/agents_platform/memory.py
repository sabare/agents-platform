"""Mandatory memory, loaded with SQL on every activation; no embedding dependency.

Task memory uses normalized rows with a bounded active projection. Immutable Sources and
ActivityEvents preserve how it changed. Instructions have a separate revision ledger.
"""

import hashlib
import json
from uuid import UUID

from sqlalchemy import select, text

from agents_platform.memory_lifecycle import is_current, lifecycle_view, task_memory_view
from agents_platform.models import CaseRestriction, Instruction, Task, TaskMemoryItem


async def policy_lock(session, tenant_id, *, publishing=False):
    """Concurrent activations share this lock; policy publication takes it exclusively.

    Always acquire before task/case locks. The lock lasts through a simulated action,
    closing the check/action race without serializing activations with each other.
    """
    name = "pg_advisory_xact_lock" if publishing else "pg_advisory_xact_lock_shared"
    key = int.from_bytes(hashlib.sha256(str(tenant_id).encode()).digest()[:8], "big", signed=True)
    await session.execute(text(f"SELECT {name}(:key)"), {"key": key})


async def instructions_for(session, tenant_id, provider_ids):
    rows = (
        await session.scalars(
            select(Instruction)
            .where(
                Instruction.tenant_id == tenant_id,
                Instruction.status == "ACTIVE",
                Instruction.provider_contact_id.is_(None)
                | Instruction.provider_contact_id.in_(provider_ids),
            )
            .order_by(Instruction.id)
        )
    ).all()
    return [row for row in rows if is_current(row)]


def instruction_view(row):
    return {
        "id": str(row.id),
        "revision": row.revision,
        "scope": row.scope,
        "provider_contact_id": str(row.provider_contact_id) if row.provider_contact_id else None,
        "text": row.text,
        "status": row.status,
        **lifecycle_view(row),
    }


async def assemble_memory(session, tenant_id, provider_ids, tasks, case_id):
    """Shared mandatory lane. Never hide active restrictions behind recent-event limits."""
    instructions = [
        instruction_view(i) for i in await instructions_for(session, tenant_id, provider_ids)
    ]
    rows = (
        await session.scalars(
            select(TaskMemoryItem).where(
                TaskMemoryItem.tenant_id == tenant_id,
                TaskMemoryItem.case_id == case_id,
                TaskMemoryItem.task_id.in_([task.id for task in tasks]),
                TaskMemoryItem.status == "OPEN",
            )
        )
    ).all()
    items = [task_memory_view(row) for row in rows if is_current(row)]
    restrictions = (
        await session.scalars(
            select(CaseRestriction).where(
                CaseRestriction.tenant_id == tenant_id,
                CaseRestriction.case_id == case_id,
                CaseRestriction.status == "OPEN",
                CaseRestriction.provider_contact_id.is_(None)
                | CaseRestriction.provider_contact_id.in_(provider_ids),
            )
        )
    ).all()
    items.extend(restriction_view(r) for r in restrictions if is_current(r))
    items.sort(key=lambda i: i["id"])
    selected = {
        "instructions": instructions,
        "operational_items": [i for i in items if i["kind"] != "NOTE"],
        "optional_notes": [i for i in items if i["kind"] == "NOTE"],
    }
    fingerprint = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    return {
        **selected,
        "manifest": {
            "schema": "mandatory-memory.v1",
            "fingerprint": fingerprint,
            "instruction_revisions": [
                {"id": i["id"], "revision": i["revision"]} for i in instructions
            ],
            "item_revisions": [{"id": i["id"], "revision": i["revision"]} for i in items],
            "source_ids": sorted({i["source_id"] for i in items}),
            "complete": True,
            "retrieval": "scoped SQL; no semantic search",
        },
    }


def stop_contact(memory):
    return [i for i in memory["operational_items"] if i["kind"] == "STOP_CONTACT"]


async def memory_for_task(session, task, *, adapter=None, snapshot=None):
    if task.kind == "GROUP":
        providers = ()
    else:
        if adapter is None:
            from agents_platform.domains import default_registry

            adapter = default_registry().for_task(task)
        snapshot = snapshot or await adapter.load(session, task)
        providers = snapshot.instruction_provider_ids
    return await assemble_memory(session, task.tenant_id, providers, [task], task.case_id)


async def memory_for_case(session, coord):
    # This query is deliberately independent of the supervisor's bounded task-history list.
    tasks = (
        await session.scalars(
            select(Task).where(Task.tenant_id == coord.tenant_id, Task.case_id == coord.case_id)
        )
    ).all()
    providers = [UUID(t["provider_contact_id"]) for t in coord.objective_json.get("targets", [])]
    return await assemble_memory(session, coord.tenant_id, providers, tasks, coord.case_id)


def restriction_view(row):
    return {
        "id": str(row.id),
        "kind": "STOP_CONTACT",
        "scope": "CASE",
        "task_id": None,
        "provider_contact_id": str(row.provider_contact_id) if row.provider_contact_id else None,
        "text": row.text,
        "status": row.status,
        "revision": row.revision,
        **lifecycle_view(row),
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "superseded_by_id": str(row.superseded_by_id) if row.superseded_by_id else None,
        "source_id": str(row.source_id),
        "resolution_source_id": str(row.resolution_source_id) if row.resolution_source_id else None,
        "resolution_reason": row.resolution_reason,
    }

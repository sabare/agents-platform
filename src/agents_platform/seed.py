"""Explicit, idempotent fictional fixture setup; does not run automatically at startup."""

import argparse
import asyncio
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import (
    AgentVersion,
    Case,
    CaseContact,
    Contact,
    Firm,
    Instruction,
    InstructionRevision,
    Task,
)
from agents_platform.registry import definition_hash, load_definition

DEMO_FIRM_ID = UUID("00000000-0000-4000-8000-000000000001")


async def seed_demo(session: AsyncSession, firm_id: UUID = DEMO_FIRM_ID) -> dict:
    def identity(name: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"hellocounsel-demo/{firm_id}/{name}")

    case_id, patient_id, provider_id, parent_id = (
        identity(name) for name in ("case", "patient", "provider", "parent-task")
    )
    async with session.begin():
        for agent_key, version in (
            ("medical_records_collector", 1),
            ("medical_records_collector", 2),
            ("case_supervisor", 1),
            ("client_check_in", 1),
        ):
            definition = load_definition(agent_key, version)
            await session.execute(
                insert(AgentVersion)
                .values(
                    agent_key=definition["agent_key"],
                    version=version,
                    definition_hash=definition_hash(definition),
                    definition_snapshot=definition,
                )
                .on_conflict_do_nothing()
            )
            published = await session.get(AgentVersion, (definition["agent_key"], version))
            if published.definition_hash != definition_hash(definition):
                raise ValueError(
                    "Published agent version changed; publish a new version explicitly"
                )
        await session.execute(
            insert(Firm)
            .values(id=firm_id, name="Fictional Example Firm", timezone="America/New_York")
            .on_conflict_do_nothing()
        )
        await session.execute(
            insert(Case)
            .values(
                id=case_id,
                tenant_id=firm_id,
                display_number="DEMO-001",
                title="Fictional records collection case",
            )
            .on_conflict_do_nothing()
        )
        for contact_id, name, kind, roles in (
            (patient_id, "Example Client", "PERSON", ["CLIENT", "PATIENT"]),
            (provider_id, "Example Hospital", "ORGANIZATION", ["MEDICAL_PROVIDER"]),
        ):
            await session.execute(
                insert(Contact)
                .values(id=contact_id, tenant_id=firm_id, display_name=name, kind=kind)
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(CaseContact)
                .values(tenant_id=firm_id, case_id=case_id, contact_id=contact_id, roles=roles)
                .on_conflict_do_nothing()
            )
        for scope, text in (
            (
                "FIRM",
                "Report meaningful changes. Keep client reports distinct from verified facts.",
            ),
            (
                "PROVIDER",
                "Example Hospital accepts simulated records requests; use the request reference.",
            ),
            (
                "FIRM_PROVIDER",
                "Use the existing request for follow-ups; do not create duplicate requests.",
            ),
        ):
            await session.execute(
                insert(Instruction)
                .values(
                    id=identity(f"instruction/{scope}"),
                    tenant_id=firm_id,
                    scope=scope,
                    provider_contact_id=None if scope == "FIRM" else provider_id,
                    text=text,
                )
                .on_conflict_do_nothing()
            )
        for scope, text in (
            await session.execute(
                select(Instruction.scope, Instruction.text).where(
                    Instruction.tenant_id == firm_id, Instruction.revision == 1
                )
            )
        ).all():
            await session.execute(
                insert(InstructionRevision)
                .values(
                    tenant_id=firm_id,
                    instruction_id=identity(f"instruction/{scope}"),
                    revision=1,
                    scope=scope,
                    provider_contact_id=None if scope == "FIRM" else provider_id,
                    text=text,
                    approved_by="demo-seed",
                    reason="Initial demo policy",
                )
                .on_conflict_do_nothing()
            )
        await session.execute(
            insert(Task)
            .values(
                id=parent_id,
                tenant_id=firm_id,
                case_id=case_id,
                kind="GROUP",
                task_type="records.group",
                objective="Collect required medical records",
                input_schema="group.v1",
                input_json={},
                progress_json={},
                completion_json={},
                status="PENDING",
            )
            .on_conflict_do_nothing()
        )
    return {
        "firm_id": str(firm_id),
        "assignment": {
            "case_id": str(case_id),
            "parent_task_id": str(parent_id),
            "patient_contact_id": str(patient_id),
            "provider_contact_id": str(provider_id),
            "agent_version": 2,
            "objective": "Collect medical records and imaging from Example Hospital",
            "requirements": [
                {"record_type": "RECORDS", "all_dates": True},
                {"record_type": "IMAGING", "all_dates": True},
            ],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed fictional task-creation fixtures")
    parser.add_argument("--firm-id", type=UUID, default=DEMO_FIRM_ID)
    args = parser.parse_args()

    async def run():
        database = Database(get_settings().database_url)
        try:
            async with database.sessions() as session:
                print(json.dumps(await seed_demo(session, args.firm_id), indent=2))
        finally:
            await database.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()

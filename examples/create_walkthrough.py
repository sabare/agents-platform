"""Create an idempotent, fictional two-hospital workspace for manual walkthroughs.

Run with the API container's configuration:
  docker compose exec -T api python - < examples/create_walkthrough.py
No assignments or outreach are started.
"""

import asyncio
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.dialects.postgresql import insert

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import Case, CaseContact, Contact, Firm
from agents_platform.seed import seed_demo

FIRM_ID = uuid5(NAMESPACE_URL, "hellocounsel/platform-walkthrough/v1")
PROVIDER_ID = uuid5(FIRM_ID, "riverside-medical-center")


async def main():
    db = Database(get_settings().database_url)
    try:
        async with db.sessions() as session:
            fixture = await seed_demo(session, FIRM_ID)
        async with db.sessions.begin() as session:
            firm = await session.get(Firm, FIRM_ID)
            firm.name = "Platform walkthrough"
            case_id = UUID(fixture["assignment"]["case_id"])
            case = await session.get(Case, case_id)
            case.title = "Hospital records and client check-ins"
            case.display_number = "CASE-001"
            client = await session.get(Contact, UUID(fixture["assignment"]["patient_contact_id"]))
            client.display_name = "Jordan Lee"
            await session.execute(
                insert(Contact)
                .values(
                    id=PROVIDER_ID,
                    tenant_id=FIRM_ID,
                    display_name="Riverside Medical Center",
                    kind="ORGANIZATION",
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                insert(CaseContact)
                .values(
                    tenant_id=FIRM_ID,
                    case_id=case_id,
                    contact_id=PROVIDER_ID,
                    roles=["MEDICAL_PROVIDER"],
                )
                .on_conflict_do_nothing()
            )
        print(
            json.dumps(
                {
                    "url": f"http://localhost:8000/?firm={FIRM_ID}",
                    "case": "CASE-001: Hospital records and client check-ins",
                    "client": "Jordan Lee",
                    "hospitals": ["Example Hospital", "Riverside Medical Center"],
                },
                indent=2,
            )
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

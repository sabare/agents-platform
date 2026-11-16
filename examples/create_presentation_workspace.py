"""Create a separate, repeat-safe interview workspace without starting agent work.

Run: docker compose exec -T api python - < examples/create_presentation_workspace.py
"""

import argparse
import asyncio
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from agents_platform.config import get_settings
from agents_platform.db import Database
from agents_platform.models import Case, Contact, Firm, Task
from agents_platform.seed import seed_demo


async def main(
    workspace_key="hellocounsel/interview-presentation/v1", name="Interview presentation"
):
    firm_id = uuid5(NAMESPACE_URL, workspace_key)
    database = Database(get_settings().database_url)
    try:
        async with database.sessions() as session:
            fixture = await seed_demo(session, firm_id)
        async with database.sessions.begin() as session:
            firm = await session.get(Firm, firm_id)
            firm.name = name
            case = await session.get(Case, UUID(fixture["assignment"]["case_id"]))
            case.display_number = "CASE-101"
            case.title = "Jordan Lee — records and client check-in"
            client = await session.get(Contact, UUID(fixture["assignment"]["patient_contact_id"]))
            client.display_name = "Jordan Lee"
            group = await session.get(Task, UUID(fixture["assignment"]["parent_task_id"]))
            if group.task_type != "case.coordination":
                group.objective = "Jordan Lee — medical records"
        print(
            json.dumps(
                {
                    "workspace": name,
                    "firm_id": str(firm_id),
                    "url": f"http://localhost:8000/?firm={firm_id}",
                    "case": "CASE-101 · Jordan Lee — records and client check-in",
                    "client": "Jordan Lee",
                    "provider": "Example Hospital",
                    "note": (
                        "Contacts and case are ready. Existing work is preserved; "
                        "setup does not reset tasks or start outreach."
                    ),
                },
                indent=2,
            )
        )
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", default="hellocounsel/interview-presentation/v1")
    parser.add_argument("--name", default="Interview presentation")
    args = parser.parse_args()
    asyncio.run(main(args.key, args.name))

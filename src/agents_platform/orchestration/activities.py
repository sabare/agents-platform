import asyncio

from temporalio import activity

from agents_platform.db import Database
from agents_platform.orchestration.contracts import ProbeResult


class InfrastructureActivities:
    def __init__(self, database: Database) -> None:
        self.database = database

    @activity.defn
    async def check_database(self) -> ProbeResult:
        async with asyncio.timeout(5):
            version = await self.database.check()
        return ProbeResult(postgres="ok", pgvector_version=version)

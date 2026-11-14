import asyncio
from datetime import timedelta

from dotenv import load_dotenv
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from agents_platform.capabilities import discover_capabilities
from agents_platform.check_in_series import SeriesActivities
from agents_platform.config import get_settings
from agents_platform.coordination import SupervisorActivities
from agents_platform.db import Database
from agents_platform.dispatch import dispatch_loop
from agents_platform.execution import TaskActivities
from agents_platform.human_request_handlers import backfill_blocked_requests
from agents_platform.memory_history import MemoryActivities
from agents_platform.monthly_check_ins import MonthlyActivities
from agents_platform.orchestration.activities import InfrastructureActivities
from agents_platform.orchestration.client import connect_temporal
from agents_platform.orchestration.workflows import (
    CaseWorkflow,
    CheckInSeriesWorkflow,
    InfrastructureProbe,
    MemoryIndexWorkflow,
    MonthlyCheckInWorkflow,
    TaskWorkflow,
)
from agents_platform.recovery import backfill_incidents
from agents_platform.registry import publish_definitions
from agents_platform.telemetry import configure_logging, emit


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings.database_url)
    try:
        await database.check()
        catalog = discover_capabilities()
        async with database.sessions.begin() as session:
            await publish_definitions(session, catalog)
        await backfill_incidents(database)
        await backfill_blocked_requests(database)
        client = await connect_temporal(settings)
        activities = InfrastructureActivities(database)
        task_activities = TaskActivities(database, settings, catalog.adapters)
        supervisor = SupervisorActivities(database, settings, catalog)
        memory = MemoryActivities(database, settings)
        series = SeriesActivities(database)
        monthly = MonthlyActivities(database)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[
                CaseWorkflow,
                InfrastructureProbe,
                TaskWorkflow,
                MemoryIndexWorkflow,
                MonthlyCheckInWorkflow,
                CheckInSeriesWorkflow,
            ],
            activities=[
                memory.index_memory,
                series.advance_series,
                monthly.advance_monthly,
                series.fail_series,
                supervisor.coordinate_case,
                supervisor.fail_supervisor,
                activities.check_database,
                task_activities.execute_task,
                task_activities.fail_task,
            ],
            graceful_shutdown_timeout=timedelta(seconds=15),
        )
        emit("worker.started", task_queue=settings.temporal_task_queue)
        try:
            await client.start_workflow(
                MemoryIndexWorkflow.run,
                id="memory-index-v1",
                task_queue=settings.temporal_task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass
        async with asyncio.TaskGroup() as group:
            group.create_task(worker.run())
            group.create_task(dispatch_loop(database, client, settings.temporal_task_queue))
    finally:
        await database.close()


def main() -> None:
    load_dotenv()
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

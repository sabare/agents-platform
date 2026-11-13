from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from agents_platform.orchestration.contracts import ProbeInput, ProbeResult


@workflow.defn
class InfrastructureProbe:
    """Infrastructure-only check: durable timer followed by database activity."""

    @workflow.run
    async def run(self, request: ProbeInput) -> ProbeResult:
        await workflow.sleep(timedelta(seconds=request.delay_seconds))
        return await workflow.execute_activity(
            "check_database",
            result_type=ProbeResult,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=5),
                maximum_attempts=3,
            ),
        )


@workflow.defn
class TaskWorkflow:
    """Durable control flow only. Agent, tools, context, and database live in an activity."""

    def __init__(self):
        self.woken = False

    @workflow.signal
    def wake(self):
        self.woken = True

    @workflow.run
    async def run(self, request: dict) -> dict:
        cycle, trigger = request.get("cycle", 0), request.get("trigger", "created")
        while True:
            self.woken = False
            activation = {**request, "cycle": cycle, "trigger": trigger}
            try:
                outcome = await workflow.execute_activity(
                    "execute_task",
                    activation,
                    result_type=dict,
                    start_to_close_timeout=timedelta(minutes=5),
                    schedule_to_close_timeout=timedelta(minutes=6),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3, initial_interval=timedelta(seconds=1)
                    ),
                )
            except ActivityError:
                await workflow.execute_activity(
                    "fail_task",
                    activation,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                return {"status": "FAILED"}
            if outcome["status"] in ("COMPLETED", "CANCELLED", "FAILED"):
                return outcome
            cycle += 1
            trigger = "event"
            if outcome["status"] == "CONTINUE" or self.woken:
                pass
            elif outcome["status"] == "WAITING":
                seconds = max(
                    0, (datetime.fromisoformat(outcome["wake_at"]) - workflow.now()).total_seconds()
                )
                try:
                    await workflow.wait_condition(
                        lambda: self.woken, timeout=timedelta(seconds=seconds)
                    )
                except TimeoutError:
                    trigger = "timer"
            else:
                await workflow.wait_condition(lambda: self.woken)
            if cycle % 100 == 0:
                workflow.continue_as_new({**request, "cycle": cycle, "trigger": trigger})


@workflow.defn
class CaseWorkflow:
    """One sleeping coordinator per case. Signals only wake the durable inbox reader."""

    def __init__(self):
        self.woken = False

    @workflow.signal
    def wake(self):
        self.woken = True

    @workflow.run
    async def run(self, request: dict):
        cycle, trigger = request.get("cycle", 0), "event"
        while True:
            self.woken = False
            activation = {**request, "cycle": cycle, "trigger": trigger}
            try:
                outcome = await workflow.execute_activity(
                    "coordinate_case",
                    activation,
                    result_type=dict,
                    start_to_close_timeout=timedelta(minutes=2),
                    schedule_to_close_timeout=timedelta(minutes=3),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                await workflow.execute_activity(
                    "fail_supervisor",
                    activation,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                outcome = {"status": "WAITING"}
            if outcome["status"] == "CLOSED":
                return outcome
            cycle += 1
            trigger = "event"
            if outcome["status"] == "CONTINUE":
                trigger = "continue"
            elif not self.woken:
                if outcome.get("wake_at"):
                    seconds = max(
                        0,
                        (
                            datetime.fromisoformat(outcome["wake_at"]) - workflow.now()
                        ).total_seconds(),
                    )
                    try:
                        await workflow.wait_condition(
                            lambda: self.woken, timeout=timedelta(seconds=seconds)
                        )
                    except TimeoutError:
                        trigger = "timer"
                else:
                    await workflow.wait_condition(lambda: self.woken)
            if cycle % 100 == 0 or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new({**request, "cycle": cycle})


@workflow.defn
class MemoryIndexWorkflow:
    """Derived work only. A durable timer catches new/backfilled sources after restarts."""

    @workflow.run
    async def run(self):
        for _ in range(100):
            try:
                result = await workflow.execute_activity(
                    "index_memory",
                    result_type=dict,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
                delay = 1 if result["indexed_sources"] == 30 else 15
            except ActivityError:
                delay = 60
            await workflow.sleep(timedelta(seconds=delay))
        workflow.continue_as_new()

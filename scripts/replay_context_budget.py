"""Read-only replay of saved context packing: no model calls and no business actions."""

import argparse
import asyncio
import json
from uuid import UUID

from agents import Agent
from agents.run import CallModelData, ModelInputData
from pydantic import TypeAdapter

from agents_platform.config import Settings
from agents_platform.context import PLATFORM_INSTRUCTIONS, ModelBudget, dumps, pack_context, tokens
from agents_platform.db import Database
from agents_platform.execution import ActivationContext, TaskActivities
from agents_platform.models import AgentVersion, Execution, Task


async def replay(execution_id):
    settings = Settings()
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            execution = await session.get(Execution, execution_id)
            if execution is None:
                raise ValueError("Saved execution not found")
            task = await session.get(Task, execution.task_id)
            definition = (
                await session.get(AgentVersion, (task.agent_key, task.agent_version))
            ).definition_snapshot
            worker = TaskActivities(database, settings)
            adapter = worker.adapters.for_task(task)
            context = execution.context_json
            original = dumps(context)
            activation = ActivationContext(
                task.tenant_id,
                task.id,
                execution.id,
                execution.attempt,
                context,
                definition,
                execution.mode,
                adapter,
            )
            agent = Agent(
                name=definition["name"],
                instructions=definition["instructions"] + PLATFORM_INSTRUCTIONS,
                tools=worker._tools(activation),
                output_type=adapter.decision_type,
            )
            schemas = {
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.params_json_schema,
                    }
                    for t in agent.tools
                ],
                "output": TypeAdapter(agent.output_type).json_schema(),
            }
            reserve = tokens(agent.instructions) + tokens(schemas) + 1024
            available = max(0, settings.context_input_tokens - reserve - 1 - 64 - 512)
            single_pass = pack_context(context, available)
            before = reserve + tokens([{"role": "user", "content": dumps(single_pass)}]) + 64
            budget = ModelBudget(context, settings)
            result = budget.filter(
                CallModelData(
                    model_data=ModelInputData(
                        input=[{"role": "user", "content": json.dumps(context)}],
                        instructions=agent.instructions,
                    ),
                    agent=agent,
                    context=None,
                )
            )
            packed = json.loads(result.input[0]["content"])
            report = {
                "execution_id": str(execution_id),
                "input_limit": settings.context_input_tokens,
                "old_single_pass_estimate": before,
                "new_estimate": budget.calls[-1],
                "task_preserved": packed["task"] == context["task"],
                "domain_preserved": packed["domain"] == context["domain"],
                "mandatory_memory_preserved": (
                    packed["memory"]["operational_items"] == context["memory"]["operational_items"]
                    and packed["memory"]["instructions"] == context["memory"]["instructions"]
                ),
                "saved_snapshot_unchanged": dumps(context) == original,
                "selection": packed.get("context_selection"),
                "model_calls": 0,
                "business_actions": 0,
            }
            print(json.dumps(report, indent=2))
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execution_id", type=UUID)
    asyncio.run(replay(parser.parse_args().execution_id))

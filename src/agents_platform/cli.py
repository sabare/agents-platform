import argparse
import asyncio
import os

from agents import RunConfig, Runner
from dotenv import load_dotenv

from agents_platform.supervisor import create_supervisor_agent


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run the HelloCounsel case supervisor.")
    parser.add_argument(
        "assignment", nargs="?", help="Case context and assignment for the supervisor."
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate agent wiring without an API call."
    )
    args = parser.parse_args()
    supervisor = create_supervisor_agent(os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    if args.check:
        print(f"{supervisor.name}: {', '.join(tool.name for tool in supervisor.tools)}")
        return
    if not args.assignment:
        parser.error("provide an assignment, or use --check")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("set OPENAI_API_KEY in your environment or .env")

    async def run() -> None:
        result = await Runner.run(
            supervisor,
            args.assignment,
            max_turns=10,
            run_config=RunConfig(tracing_disabled=True),
        )
        print(result.final_output)

    asyncio.run(run())


if __name__ == "__main__":
    main()

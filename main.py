"""CLI entry point.

uv run python main.py "Plan a family-friendly Saturday day trip within 45 minutes of Chislehurst"
uv run python main.py --demo          # offline, uses schemas/examples.py
uv run python main.py --demo --wet    # offline, rainy-day conflict resolution
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # before importing modules that read WC_* env vars

from agents.base import AgentSpec  # noqa: E402
from hooks import TraceLogger  # noqa: E402
from orchestrator import Supervisor  # noqa: E402
from schemas import examples  # noqa: E402

DEFAULT_REQUEST = (
    "Plan a family-friendly Saturday day trip within 45 minutes of "
    "Chislehurst, South East London, with lunch options"
)


def demo_runner(wet: bool):
    fixtures = {
        "supervisor": examples.CONSTRAINTS,
        "venue_agent": examples.VENUES,
        "logistics_agent": examples.LOGISTICS_WET if wet else examples.LOGISTICS_DRY,
        "food_agent": examples.FOOD,
    }

    async def run(spec: AgentSpec, prompt: str, trace: TraceLogger):
        trace.write("demo_agent", agent=spec.name)
        await asyncio.sleep(0.1)  # all three subagents "run" at the same time
        return fixtures[spec.name]

    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekend & Logistics Coordinator")
    ap.add_argument("request", nargs="?", default=DEFAULT_REQUEST, help="natural-language request")
    ap.add_argument("--demo", action="store_true", help="run offline with sample subagent outputs")
    ap.add_argument("--wet", action="store_true", help="with --demo: use a rainy forecast")
    args = ap.parse_args(argv)

    if args.demo:
        supervisor = Supervisor(runner=demo_runner(args.wet))
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            msg = "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            print(msg, file=sys.stderr)
            return 2
        supervisor = Supervisor()

    plan, path = asyncio.run(supervisor.plan(args.request))
    print(path.read_text(encoding="utf-8"))
    print(f"\nSaved to {path}\nTrace: {supervisor.trace.path} (run_id={supervisor.trace.run_id})")
    return 0 if plan.primary_venue else 1


if __name__ == "__main__":
    raise SystemExit(main())

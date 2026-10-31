"""Show Quiff searching live, model-free.

    uv run python spikes/demo_search.py --query Paris --headed

Watch Chrome: it clicks Search, types the query, presses Enter, and opens the
article. The 'brain' is MatchDecider (the scripted stand-in for the decision
model); the eyes, hands, loop and logging are the real engine.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from quiff.decide import MatchDecider, MatchStep
from quiff.learn import StepLogger
from quiff.loop import FastLoop
from quiff.observe.browser import BrowserObserver


async def main_async(args: argparse.Namespace) -> None:
    steps = [
        MatchStep("click", "link", "search"),
        MatchStep("type", "searchbox", "search", (args.query,)),
        MatchStep("click", "button", "search"),
        MatchStep("click", "link", args.query),
    ]
    observer = BrowserObserver(headless=not args.headed)
    logger = StepLogger(Path("runs") / f"demo-{time.strftime('%Y-%m-%d-%H%M%S')}", "demo-search")
    loop = FastLoop(observer, MatchDecider(steps), logger)
    goal = f"Search Wikipedia for '{args.query}' and open its article."
    try:
        result = await loop.run(goal, start_url="https://en.wikipedia.org", subgoal=goal)
        info = await observer.target_info()
        print(f"steps   {result.steps} · {result.elapsed_s}s · {result.stop_reason}")
        print(f"landed  {info.get('title', '')} — {info.get('url', '')}")
    finally:
        await observer.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="Paris")
    parser.add_argument("--headed", action="store_true")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()

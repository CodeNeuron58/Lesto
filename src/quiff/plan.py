"""Plan and escalate: a frontier model splits the task into subgoals once at the start.

After that it is called only on low confidence, a failed check, or a detected loop.
"""

from __future__ import annotations

import asyncio

from .models.meta import ChatClient, muse_spark

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "subgoals": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-5 short subgoals, in order, each doable in one or two steps",
        }
    },
    "required": ["subgoals"],
}

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {"advice": {"type": "string"}},
    "required": ["advice"],
}


async def plan(goal: str, client: ChatClient | None = None) -> list[str]:
    """One high-reasoning call turns the goal into subgoals the fast loop works through."""
    client = client or muse_spark()
    prompt = (
        f"Split this computer-use goal into 1-5 short subgoals, in order. Each subgoal "
        f"should be doable in one or two actions (click, type, press, select). "
        f"Goal: {goal}"
    )
    parsed, reply = await asyncio.to_thread(
        client.structured, prompt, PLAN_SCHEMA, name="plan", effort="high", max_tokens=300
    )
    subgoals = [s.strip() for s in parsed.get("subgoals", []) if s and s.strip()]
    return subgoals or [goal]


async def rescue(goal: str, subgoal: str, problem: str, client: ChatClient | None = None) -> str:
    """Ask the frontier model what to do differently when the fast path is stuck."""
    client = client or muse_spark()
    prompt = (
        "A computer-use agent is stuck.\n"
        f"Goal: {goal}\nSubgoal: {subgoal}\nProblem: {problem}\n\n"
        'Reply with JSON only: {"advice": "one or two sentences on what to try next"}'
    )
    parsed, _ = await asyncio.to_thread(
        client.structured, prompt, ADVICE_SCHEMA, name="rescue", effort="high", max_tokens=200
    )
    return parsed.get("advice", "try a different approach")

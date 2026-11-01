"""Plan: a frontier model splits the task into subgoals once at the start.

Escalation after that is handled in the loop: a second MuseDecider at high
reasoning takes over on low confidence, a failed check, or a detected loop.
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
        client.structured, prompt, PLAN_SCHEMA, name="plan", effort="high", max_tokens=1024
    )
    subgoals = [s.strip() for s in parsed.get("subgoals", []) if s and s.strip()]
    return subgoals or [goal]


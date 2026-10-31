"""Stage 3, Decide: one request to the decision model answers four typed questions.

Which action, which row, did the last step work, and is the subgoal done.
Only the goal, subgoal, filtered action table and last two actions are sent.

Until the model client lands (waiting on hackathon API credits), ScriptedDecider
answers from a fixed list of steps so the loop, acting and checking can be built
and tested with zero model calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from .observe.table import ActionTable

ACTIONS = ("click", "type", "select", "press", "done", "answer")


@dataclass(frozen=True)
class Decision:
    """What the decision model returns for one step."""

    action: str
    """One of ACTIONS: click, type, select, press, done, answer."""

    row: int | None = None
    """The action-table row the action targets; None for press/done/answer."""

    values: tuple[str, ...] = ()
    """Text to type, the option to select, or the key to press (e.g. ("Enter",))."""

    answer: str | None = None
    """The task's final answer, for answer tasks."""

    last_step_ok: bool | None = None
    """The model's read of whether the previous step did what it should."""

    subgoal_done: bool | None = None
    """The model's read of whether the current subgoal is complete."""

    confidence: float = 1.0
    """0..1. Low confidence escalates to the frontier model."""

    note: str = ""


class Decider(Protocol):
    async def decide(
        self, goal: str, subgoal: str, table: ActionTable, history: Sequence["Decision"]
    ) -> Decision: ...


@dataclass
class ScriptedDecider:
    """A stand-in decision model: replays a fixed list of decisions in order.

    Lets the engine run end to end with no model call, and gives the demo a
    deterministic path when a live model is not wanted.
    """

    steps: list[Decision] = field(default_factory=list)
    _next: int = 0

    async def decide(
        self, goal: str, subgoal: str, table: ActionTable, history: Sequence[Decision]
    ) -> Decision:
        if self._next >= len(self.steps):
            return Decision(action="done", subgoal_done=True, note="script exhausted")
        decision = self.steps[self._next]
        object.__setattr__(self, "_next", self._next + 1)
        return decision


@dataclass(frozen=True)
class MatchStep:
    """One scripted step whose row is found in the live table at decide time.

    The first row whose role and name contain the given fragments wins.
    """

    action: str
    role_contains: str = ""
    name_contains: str = ""
    values: tuple[str, ...] = ()
    answer: str | None = None
    optional: bool = False
    """When no row matches, skip silently instead of failing (e.g. a confirm
    dialog that only sometimes appears)."""


class MatchDecider:
    """A scripted decider that finds its rows in the table the way a model would.

    Deterministic, model-free, and robust to row numbering: each MatchStep is
    resolved against the current action table when its turn comes.
    """

    def __init__(self, steps: list[MatchStep]) -> None:
        self.steps = steps
        self._next = 0

    async def decide(
        self, goal: str, subgoal: str, table: ActionTable, history: Sequence[Decision]
    ) -> Decision:
        if self._next >= len(self.steps):
            return Decision(action="done", subgoal_done=True, note="script finished")
        step = self.steps[self._next]
        self._next += 1
        if step.action in ("done", "answer", "press"):
            return Decision(
                action=step.action, values=step.values, answer=step.answer, subgoal_done=True
            )
        for row in table.rows:
            if step.role_contains in row.role and step.name_contains.lower() in row.name.lower():
                return Decision(
                    action=step.action,
                    row=row.index,
                    values=step.values,
                    last_step_ok=True,
                    subgoal_done=False,
                    note=f"match {step.role_contains!r}/{step.name_contains!r}",
                )
        if step.optional:
            return Decision(action="done", subgoal_done=True,
                            note=f"optional step skipped: no row matches "
                                 f"{step.role_contains!r}/{step.name_contains!r}")
        return Decision(action="done", subgoal_done=False, confidence=0.0,
                        note=f"no row matches {step.role_contains!r}/{step.name_contains!r}")

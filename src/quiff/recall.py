"""Stage 2, Recall: replay known flows with no model call.

Record a successful run once (the resolved rows, not the row numbers), then
replay it: each step re-finds its row in the live table by role and name —
so row numbering, window sizes and small layout shifts don't matter — and the
loop's own acting and checking stages still verify every step.

The screen check is honest: if the row a step expects isn't on screen, replay
stops and says so, instead of clicking something else.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from .decide import Decision, Decider
from .observe.table import ActionTable

FLOWS_DIR = Path("runs") / "flows"


@dataclass
class FlowStep:
    action: str
    role: str
    """The resolved row's role, e.g. button."""

    name: str
    """The resolved row's name; replay re-finds by role + this name."""

    values: tuple[str, ...] = ()
    note: str = ""


@dataclass
class Flow:
    goal: str
    task_id: str
    steps: list[FlowStep] = field(default_factory=list)
    recorded_at: str = ""


def save_flow(flow: Flow, directory: Path = FLOWS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{flow.task_id}.json"
    path.write_text(json.dumps(asdict(flow), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_flow(task_id: str, directory: Path = FLOWS_DIR) -> Flow:
    path = directory / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no recorded flow for {task_id!r}; run it once first")
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = [FlowStep(values=tuple(step.pop("values")), **step)
             for step in data.pop("steps")]
    return Flow(**data, steps=steps)


class FlowRecorder:
    """Wraps any decider, records the rows it actually resolved, saves on success.

    Row numbers are never stored — the resolved role and name are what replay
    re-finds, so a flow survives layout shifts between runs.
    """

    def __init__(self, inner: Decider, task_id: str, goal: str) -> None:
        self.inner = inner
        self.flow = Flow(goal=goal, task_id=task_id,
                         recorded_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self._ran = False

    async def decide(
        self, goal: str, subgoal: str, table: ActionTable, history: Sequence[Decision]
    ) -> Decision:
        decision = await self.inner.decide(goal, subgoal, table, history)
        row = table.by_index(decision.row) if decision.row is not None else None
        if decision.action in ("click", "type", "select") and row is not None:
            # NOTE: flow files hold real typed values (replay must reproduce
            # them) and stay local under runs/, which git ignores. The JSONL
            # step logs, meant for training, redact credential-like fields.
            self.flow.steps.append(
                FlowStep(action=decision.action, role=row.role, name=row.name,
                         values=decision.values)
            )
        elif decision.action == "save_as":
            # driver skill: replay re-runs the skill, not the fumbled steps
            self.flow.steps.append(
                FlowStep(action="save_as", role="", name="", values=decision.values)
            )
        elif decision.action == "press":
            self.flow.steps.append(
                FlowStep(action="press", role="", name="", values=decision.values)
            )
        self._ran = True
        return decision

    def save_if_ran(self, succeeded: bool) -> Path | None:
        if succeeded and self._ran and self.flow.steps:
            return save_flow(self.flow)
        return None


class ReplayDecider:
    """The no-model decider: replays a recorded flow step by step.

    Every step must find its row before it acts. When the row isn't on screen
    (a dialog still building, a window still settling) the decider returns
    wait — the loop re-observes — up to the loop's wait budget. It never skips
    a recorded step: replay either does what was recorded or fails honestly.
    """

    def __init__(self, flow: Flow) -> None:
        self.flow = flow
        self._next = 0

    async def decide(
        self, goal: str, subgoal: str, table: ActionTable, history: Sequence[Decision]
    ) -> Decision:
        if self._next >= len(self.flow.steps):
            return Decision(action="done", subgoal_done=True, note="flow finished")
        step = self.flow.steps[self._next]
        if step.action in ("press", "save_as"):
            self._next += 1
            return Decision(action=step.action, values=step.values, note="replayed")
        row = _find(table, step)
        if row is None:
            return Decision(action="wait", note=f"waiting for {step.role}/{step.name[:24]!r}")
        self._next += 1
        return Decision(
            action=step.action,
            row=row.index,
            values=step.values,
            last_step_ok=True,
            subgoal_done=False,
            confidence=1.0,
            note=f"replayed {step.role}/{step.name[:24]!r}",
        )


def _find(table: ActionTable, step: FlowStep):
    for row in table.rows:
        if row.role == step.role and row.name == step.name:
            return row
    # names drift a little between runs (tab titles, modified markers)
    for row in table.rows:
        if row.role == step.role and step.name[:12] and step.name[:12] in row.name:
            return row
    return None

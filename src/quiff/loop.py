"""The fast loop that runs once per step: observe -> recall -> decide -> act -> verify.

The frontier model planned the subgoals up front; this loop only asks the small
decision model (or, for now, a scripted decider) to pick the next row. Recall
replay and frontier rescue plug into the same step order.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from .act import ActResult, act, needs_approval
from .decide import Decision, Decider
from .learn import StepLogger
from .observe.browser import BrowserObserver
from .observe.table import ActionTable
from .verify import LoopDetector

MAX_STEPS = 25
MAX_WAITS = 5
LOW_CONFIDENCE = 0.5


@dataclass
class StepRecord:
    step: int
    decision: Decision
    act: ActResult | None
    table_rows: int
    table_tokens: int
    decide_ms: int
    observe_ms: int


@dataclass
class LoopResult:
    success: bool
    steps: int
    elapsed_s: float
    stop_reason: str
    answer: str | None = None
    steps_log: list[StepRecord] = field(default_factory=list)


class FastLoop:
    """Runs the per-step loop against one open browser tab.

    `rescuer` is the frontier model's second chance: when acting fails twice in
    a row or the loop detector fires, the rescuer (if given) may take the step;
    without one the loop stops and says why.
    """

    def __init__(
        self,
        observer: BrowserObserver,
        decider: Decider,
        logger: StepLogger | None = None,
        *,
        max_steps: int = MAX_STEPS,
        rescuer: Decider | None = None,
    ) -> None:
        self.observer = observer
        self.decider = decider
        self.logger = logger
        self.max_steps = max_steps
        self.rescuer = rescuer

    async def run(
        self,
        goal: str,
        *,
        start_url: str | None = None,
        subgoal: str | None = None,
    ) -> LoopResult:
        started = time.perf_counter()
        history: list[Decision] = []
        detector = LoopDetector()
        failed_acts = 0
        answer: str | None = None

        if start_url:
            observe_start = time.perf_counter()
            table = await self.observer.open(start_url)
            observe_ms = int((time.perf_counter() - observe_start) * 1000)
        else:
            observe_start = time.perf_counter()
            table = await self.observer.read()
            observe_ms = int((time.perf_counter() - observe_start) * 1000)

        stop_reason = "max steps reached"
        success = False
        waits = 0
        force_rescue = False

        for step_no in range(1, self.max_steps + 1):
            decide_start = time.perf_counter()
            decision = await self.decider.decide(goal, subgoal or "", table, history)
            decide_ms = int((time.perf_counter() - decide_start) * 1000)

            if force_rescue or decision.confidence < LOW_CONFIDENCE:
                if self.rescuer is not None:
                    try:
                        decision = await self.rescuer.decide(goal, subgoal or "", table, history)
                        decision.note = f"{decision.note} · rescued".strip(" ·")
                    except Exception as exc:
                        decision = Decision(action="done", subgoal_done=False, confidence=0.0,
                                            note=f"rescuer failed: {exc}")
                force_rescue = False

            if decision.action == "wait":
                # The decider expects a screen that isn't there yet (a dialog
                # still building); re-observe instead of acting.
                waits += 1
                if waits > MAX_WAITS:
                    stop_reason = "waited too long for the expected screen"
                    self._log(step_no, decision, None, table, decide_ms, observe_ms)
                    break
                self._log(step_no, decision, None, table, decide_ms, observe_ms)
                await self.observer.wait_settled()
                table = await self.observer.read()
                continue

            if decision.action == "done":
                success = bool(decision.subgoal_done is not False)
                stop_reason = decision.note or "decider said done"
                self._log(step_no, decision, None, table, decide_ms, observe_ms)
                break
            if decision.action == "answer":
                answer = decision.answer
                success = answer is not None
                stop_reason = "answered"
                self._log(step_no, decision, None, table, decide_ms, observe_ms)
                break

            approval_reason = needs_approval(decision, table)
            if approval_reason:
                stop_reason = f"needs approval: {approval_reason}"
                self._log(step_no, decision, None, table, decide_ms, observe_ms)
                break

            result = await act(self.observer, table, decision)
            history.append(decision)
            self._log(step_no, decision, result, table, decide_ms, observe_ms)

            if not result.ok:
                failed_acts += 1
                if failed_acts >= 2:
                    if self.rescuer is not None:
                        force_rescue = True  # the frontier model picks the next step
                        failed_acts = 0
                    else:
                        stop_reason = f"acting failed twice: {result.detail}"
                        success = False
                        break
            else:
                failed_acts = 0

            ref = table.by_index(decision.row).ref if decision.row else decision.action
            if detector.check(decision.action, ref or "", table.title):
                if self.rescuer is not None:
                    force_rescue = True
                else:
                    stop_reason = "loop detected: same action on the same screen"
                    success = False
                    break

            observe_start = time.perf_counter()
            await self.observer.wait_settled()
            table = await self.observer.read()
            observe_ms = int((time.perf_counter() - observe_start) * 1000)

        elapsed = time.perf_counter() - started
        return LoopResult(
            success=success,
            steps=len(history),
            elapsed_s=round(elapsed, 2),
            stop_reason=stop_reason,
            answer=answer,
        )

    def _log(
        self,
        step_no: int,
        decision: Decision,
        result: ActResult | None,
        table: ActionTable,
        decide_ms: int,
        observe_ms: int,
    ) -> None:
        if self.logger is None:
            return
        self.logger.log(
            {
                "kind": "step",
                "step": step_no,
                "table": {"title": table.title, "url": table.url, "rows": len(table.rows),
                          "approx_tokens": table.approx_tokens()},
                "decision": {
                    "action": decision.action,
                    "row": decision.row,
                    "values": list(decision.values),
                    "confidence": decision.confidence,
                    "note": decision.note,
                },
                "act": None if result is None else
                       {"ok": result.ok, "detail": result.detail, "ms": result.ms},
                "latency_ms": {"observe": observe_ms, "decide": decide_ms},
            }
        )


def recent_actions(history: Sequence[Decision], count: int = 2) -> list[Decision]:
    """The last actions sent to the decider as context (the plan caps it at two)."""
    return list(history[-count:])

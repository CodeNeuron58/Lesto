"""Learn: log every step (action table, choice, confidence, result, tokens, time per stage).

These logs fine-tune the decision model and become replayable flows.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class StepLogger:
    """Appends one JSON line per step and a final summary line for the run."""

    def __init__(self, run_dir: Path, task_id: str = "adhoc") -> None:
        self.dir = run_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "steps.jsonl"
        self.task_id = task_id

    def log(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "task": self.task_id, **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def run_summary(self, record: dict) -> None:
        self.log({"kind": "run_summary", **record})

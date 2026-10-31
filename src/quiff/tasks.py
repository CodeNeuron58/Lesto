"""Load the task suite, set up sandboxes, and run one task end to end.

The suite lives in tasks/tasks.toml; the format is specified in tasks/README.md.
Desktop tasks run through the same loop once the desktop observer lands; this
module already covers the browser half.
"""

from __future__ import annotations

import shutil
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .act import ActResult
from .checks import CheckResult, PageView, run_checks
from .decide import Decision, Decider
from .learn import StepLogger
from .loop import FastLoop, LoopResult
from .observe.browser import BrowserObserver, BrowserError

TASKS_FILE = Path(__file__).resolve().parent.parent.parent / "tasks" / "tasks.toml"


@dataclass
class Task:
    id: str
    app: str
    split: str
    goal: str
    start: dict = field(default_factory=dict)
    notes: str = ""
    setup: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)


def load_tasks(path: Path = TASKS_FILE) -> list[Task]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return [
        Task(
            id=item["id"],
            app=item["app"],
            split=item["split"],
            goal=item["goal"],
            start=item.get("start", {}),
            notes=item.get("notes", ""),
            setup=item.get("setup", {}),
            checks=item.get("check", []),
        )
        for item in data.get("task", [])
    ]


def get_task(task_id: str, tasks: list[Task] | None = None) -> Task:
    tasks = tasks if tasks is not None else load_tasks()
    for task in tasks:
        if task.id == task_id or task.id.startswith(task_id):
            return task
    raise KeyError(f"no task {task_id!r}; try: quiff tasks")


def sandbox_dir(task: Task) -> Path:
    return Path.home() / "quiff-tasks" / task.id


def setup_sandbox(task: Task) -> Path:
    """Recreate the task's sandbox from its setup, exactly as specified."""
    sandbox = sandbox_dir(task)
    shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True)
    for relative in task.setup.get("dirs", []):
        (sandbox / relative).mkdir(parents=True, exist_ok=True)
    for relative, content in task.setup.get("files", {}).items():
        target = sandbox / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sandbox


class ObserverPage:
    """PageView over a live BrowserObserver, for the browser checks."""

    def __init__(self, observer: BrowserObserver) -> None:
        self._observer = observer

    async def url(self) -> str:
        return (await self._observer.target_info()).get("url", "")

    async def title(self) -> str:
        return (await self._observer.target_info()).get("title", "")

    async def body_text(self) -> str:
        result = await self._observer.connection().call(
            "Runtime.evaluate",
            expression="document.body ? document.body.innerText : ''",
            returnByValue=True,
        )
        return result.get("result", {}).get("value", "") or ""

    async def dom_property(self, selector: str, property: str):
        result = await self._observer.connection().call(
            "Runtime.evaluate",
            expression=(
                "(() => { const el = document.querySelector("
                + repr(selector)
                + "); return el ? el["
                + repr(property)
                + "] : null; })()"
            ),
            returnByValue=True,
        )
        return result.get("result", {}).get("value")


@dataclass
class TaskRun:
    task: Task
    loop: LoopResult
    checks: list[CheckResult]

    @property
    def success(self) -> bool:
        return self.loop.success and all(result.ok for result in self.checks)


async def run_task(
    task: Task,
    decider: Decider,
    observer: BrowserObserver,
    *,
    run_root: Path | None = None,
    max_steps: int = 25,
) -> TaskRun:
    """Run one browser task: sandbox, loop, then the automatic checks."""
    if task.app != "chrome":
        raise BrowserError(f"task {task.id} runs in {task.app}; only chrome tasks run for now")
    sandbox = setup_sandbox(task)
    run_dir = (run_root or Path("runs")) / f"{time.strftime('%Y-%m-%d-%H%M%S')}-{task.id}"
    logger = StepLogger(run_dir, task.id)
    started_ts = time.time()

    loop = FastLoop(observer, decider, logger, max_steps=max_steps)
    result = await loop.run(
        task.goal,
        start_url=_start_url(task),
        subgoal=task.goal,
    )
    page = ObserverPage(observer)
    checks = await run_checks(
        task.checks,
        answer=result.answer,
        page=page,
        sandbox=sandbox,
        started_ts=started_ts,
    )
    logger.run_summary(
        {
            "success": bool(result.success and all(item.ok for item in checks)),
            "steps": result.steps,
            "elapsed_s": result.elapsed_s,
            "stop_reason": result.stop_reason,
            "answer": result.answer,
            "checks": [{"type": c.check_type, "ok": c.ok, "detail": c.detail} for c in checks],
        }
    )
    return TaskRun(task=task, loop=result, checks=checks)


def _start_url(task: Task) -> str | None:
    url = task.start.get("url")
    if url:
        return url
    if task.start.get("file"):
        return (sandbox_dir(task) / task.start["file"]).resolve().as_uri()
    return None

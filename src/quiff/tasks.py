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
from .recall import FlowRecorder

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
        # The checks are the referee: the task passed iff its automatic
        # checks pass. The loop's own "done" is information, not the verdict.
        return bool(self.checks) and all(result.ok for result in self.checks)


def desktop_argv(task: Task) -> list[str]:
    """The command that starts the task's app, with the sandbox file or folder if given."""
    commands = {
        "notepad": ["notepad.exe"],
        "explorer": ["explorer.exe"],
        "calculator": ["calc.exe"],
        "excel": ["excel.exe"],
        "outlook": ["olk.exe"],
    }
    argv = list(commands.get(task.app, [task.app]))
    sandbox = sandbox_dir(task)
    if task.start.get("file"):
        argv.append(str(sandbox / task.start["file"]))
    elif task.start.get("folder"):
        folder = task.start["folder"]
        argv.append(str(sandbox if folder == "." else sandbox / folder))
    return argv


async def run_task(
    task: Task,
    decider: Decider,
    observer,
    *,
    run_root: Path | None = None,
    max_steps: int = 25,
    rescuer: Decider | None = None,
) -> TaskRun:
    """Run one task: sandbox, loop, then the automatic checks."""
    sandbox = setup_sandbox(task)
    run_dir = (run_root or Path("runs")) / f"{time.strftime('%Y-%m-%d-%H%M%S')}-{task.id}"
    logger = StepLogger(run_dir, task.id)
    started_ts = time.time()

    if task.app != "chrome":
        _preflight_desktop_app(task)

    loop = FastLoop(observer, decider, logger, max_steps=max_steps, rescuer=rescuer)
    if task.app == "chrome":
        result = await loop.run(task.goal, start_url=_start_url(task), subgoal=task.goal)
    else:
        from quiff.observe.desktop import DesktopObserver

        await observer.open(desktop_argv(task), cwd=str(sandbox))
        # A human doing the task knows their own folder — and knows that
        # saving into it means typing the full path in the Save dialog.
        # Tell the agent the same thing.
        desktop_goal = (
            f"{task.goal} (The task folder is: {sandbox}. When a Save dialog "
            f"appears, type the file's FULL path — folder and name together, "
            f"e.g. {sandbox}\filename.txt — into the File name box.)"
        )
        result = await loop.run(desktop_goal, subgoal=desktop_goal)
    page = ObserverPage(observer) if task.app == "chrome" else None
    checks = await run_checks(
        task.checks,
        answer=result.answer,
        page=page,
        sandbox=sandbox,
        started_ts=started_ts,
    )
    success = bool(result.success and all(item.ok for item in checks))
    if isinstance(decider, FlowRecorder):
        saved = decider.save_if_ran(success)
        if saved:
            logger.log({"kind": "flow_saved", "path": str(saved)})
    logger.run_summary(
        {
            "success": success,
            "steps": result.steps,
            "elapsed_s": result.elapsed_s,
            "stop_reason": result.stop_reason,
            "answer": result.answer,
            "checks": [{"type": c.check_type, "ok": c.ok, "detail": c.detail} for c in checks],
        }
    )
    return TaskRun(task=task, loop=result, checks=checks)


def _preflight_desktop_app(task: Task) -> None:
    """Close leftover instances of the task's app and clear its saved session
    state, so a run starts clean instead of restoring junk tabs from earlier
    test runs. The user's own windows of other apps are untouched."""
    import os
    import subprocess

    process_names = {"notepad": ["notepad.exe"], "explorer": [], "calculator": [],
                     "excel": ["EXCEL.EXE"], "outlook": ["olk.exe"]}
    for name in process_names.get(task.app, []):
        subprocess.run(["taskkill", "/IM", name, "/F"],
                       capture_output=True)  # fails harmlessly when none run
    if task.app == "notepad":
        tab_state = Path(os.environ.get("LOCALAPPDATA", "")) / (
            "Packages/Microsoft.WindowsNotepad_8wekyb3d8bbwe/LocalState/TabState"
        )
        if tab_state.exists():
            for junk in tab_state.glob("*.bin"):
                junk.unlink(missing_ok=True)


def _start_url(task: Task) -> str | None:
    url = task.start.get("url")
    if url:
        return url
    if task.start.get("file"):
        return (sandbox_dir(task) / task.start["file"]).resolve().as_uri()
    return None

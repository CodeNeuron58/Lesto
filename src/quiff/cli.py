"""Command-line entry point, run as `quiff` or `python -m quiff`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from quiff import __version__


def main() -> None:
    parser = argparse.ArgumentParser(prog="quiff", description="Fast hands for your AI.")
    parser.add_argument("--version", action="version", version=f"quiff {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("tasks", help="list the task suite")

    run = commands.add_parser("run", help="run one task from tasks/tasks.toml")
    run.add_argument("task_id", help="task id or prefix, e.g. b01")
    run.add_argument("--headed", action="store_true", help="show the browser window")
    run.add_argument("--max-steps", type=int, default=25)

    observe = commands.add_parser("observe", help="print the action table for a URL")
    observe.add_argument("url")
    observe.add_argument("--headed", action="store_true")

    args = parser.parse_args()
    try:
        asyncio.run(_dispatch(args))
    except KeyError as exc:
        raise SystemExit(str(exc.args[0]))
    except Exception as exc:
        raise SystemExit(f"quiff: {exc}")


async def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "tasks":
        from quiff.tasks import load_tasks

        for task in load_tasks():
            print(f"{task.id:22} {task.split:6} {task.app:10} {task.goal}")
        return

    if args.command == "observe":
        from quiff.observe.browser import BrowserObserver

        observer = BrowserObserver(headless=not args.headed)
        try:
            table = await observer.open(args.url)
            print(table.render())
            print(f"\n{len(table.rows)} rows · ~{table.approx_tokens()} tokens · "
                  f"{table.read_ms} ms read")
        finally:
            await observer.stop()
        return

    if args.command == "run":
        from quiff.decide import MatchDecider, MatchStep
        from quiff.tasks import get_task, load_tasks, run_task

        task = get_task(args.task_id, load_tasks())
        decider = MatchDecider(_script_for(task))
        if task.app == "chrome":
            from quiff.observe.browser import BrowserObserver

            observer = BrowserObserver(headless=not args.headed)
        else:
            from quiff.observe.desktop import DesktopObserver

            observer = DesktopObserver()
        try:
            run = await run_task(task, decider, observer, max_steps=args.max_steps)
        finally:
            await observer.stop()
        print(f"task     {run.task.id} ({run.task.split})")
        print(f"steps    {run.loop.steps} · {run.loop.elapsed_s}s · {run.loop.stop_reason}")
        if run.loop.answer:
            print(f"answer   {run.loop.answer}")
        for check in run.checks:
            print(f"check    {'PASS' if check.ok else 'FAIL'} [{check.check_type}] {check.detail}")
        print(f"result   {'PASS' if run.success else 'FAIL'}")
        if not run.success:
            raise SystemExit(1)
        return


def _script_for(task) -> list[MatchStep]:
    """The model-free scripts for the dev tasks the loop already supports.

    These stand in for the decision model until the Meta API key lands; each is
    the path a competent model would take, expressed as row matches.
    """
    from quiff.decide import MatchStep

    scripts: dict[str, list[MatchStep]] = {
        "b01-google-search": [
            MatchStep("type", "combobox", "search", ("langchain",)),
            MatchStep("press", values=("Enter",)),
        ],
        "b02-wikipedia-article": [
            # The header search input renders 0x0 until the site's JS moves it,
            # so go through the Search page like a person would.
            MatchStep("click", "link", "search"),
            MatchStep("type", "searchbox", "search", ("Kolkata",)),
            MatchStep("press", values=("Enter",)),
            MatchStep("click", "link", "Kolkata"),
        ],
        "b05-test-login": [
            MatchStep("type", "textbox", "user", ("tomsmith",)),
            MatchStep("type", "textbox", "pass", ("SuperSecretPassword!",)),
            MatchStep("click", "button", "login"),
        ],
    }
    # Desktop: d01 — type into Notepad's document, save into the sandbox via the dialog.
    if task.app != "chrome":
        from quiff.tasks import sandbox_dir

        target = sandbox_dir(task) / "hello.txt"
        return [
            MatchStep("type", "document", "", ("Hello from Quiff",)),
            MatchStep("press", values=("ctrl+s",)),
            # The Save-As dialog becomes the app's front window; the loop reads it next.
            MatchStep("type", "edit", "file name", (str(target),)),
            MatchStep("press", values=("Enter",)),
            # Only appears when the target file already exists.
            MatchStep("click", "button", "yes", optional=True),
        ]
    for task_id, steps in scripts.items():
        if task.id == task_id or task.id.startswith(task_id):
            return steps
    raise KeyError(f"no model-free script for {task.id}; the decision model takes over here")

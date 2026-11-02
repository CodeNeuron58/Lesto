"""The race: screenshot agent vs Quiff's action table vs replay, same task.

    uv run python spikes/race.py --runs 3

Slow mode is how today's frontier agents work: screenshot every step, send it
to a big model, click pixel coordinates from the answer. Quiff mode is the
action table + minimal decisions. Replay is a recorded flow with no model
calls. Same task, same checks, same browser. Results go to runs/race-*.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from quiff.checks import run_checks
from quiff.decide import Decision
from quiff.loop import FastLoop
from quiff.learn import StepLogger
from quiff.models.meta import MuseDecider, muse_spark, load_env
from quiff.observe.browser import BrowserObserver
from quiff.recall import ReplayDecider, load_flow
from quiff.tasks import ObserverPage, get_task, load_tasks, sandbox_dir, setup_sandbox

TASK_ID = "b05-test-login"


# ---------------------------------------------------------------- slow mode
# A faithful stand-in for a screenshot-based frontier agent: it never sees the
# action table — only pixels — and clicks coordinates the model reads off the
# image. One big-model call per step, no matter how simple the step is.

SLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["click", "type", "press", "done"]},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "text": {"type": "string"},
    },
    "required": ["action", "x", "y", "text"],
}


def slow_prompt(goal: str, width: int, height: int, history: list[str]) -> str:
    recent = "\n".join(history[-3:]) or "(none yet)"
    return (
        f"You are a screenshot-based computer agent. The viewport is {width}x{height} "
        f"pixels; the attached screenshot matches these coordinates 1:1.\n"
        f"Goal: {goal}\n\nActions taken so far:\n{recent}\n\n"
        'Reply with JSON only: {"action": "click"|"type"|"press"|"done", "x": pixel, '
        '"y": pixel, "text": "..."} — click at an element\'s centre; "type" clicks the '
        'field first then types "text"; "press" presses the key named in "text" '
        '(e.g. "Enter"); "done" when the goal is complete. Read coordinates off the '
        "screenshot."
    )


async def slow_act(observer, action: str, x: int, y: int, text: str) -> None:
    cdp = observer.connection()
    if x or y:
        for params in (
            dict(type="mousePressed", x=x, y=y, button="left", clickCount=1),
            dict(type="mouseReleased", x=x, y=y, button="left", clickCount=1),
        ):
            await cdp.call("Input.dispatchMouseEvent", **params)
    if action == "type" and text:
        await cdp.call("Input.insertText", text=text)
    if action == "press":
        vk = 13 if "enter" in text.lower() else 0
        if vk:
            await cdp.call("Input.dispatchKeyEvent", type="rawKeyDown", key="Enter",
                           windowsVirtualKeyCode=13, code="Enter", text="\r")
            await cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Enter",
                           windowsVirtualKeyCode=13, code="Enter")


async def slow_run(client, observer, task, max_steps: int = 12) -> dict:
    started = time.perf_counter()
    goal = task.goal
    sandbox = sandbox_dir(task)
    page = ObserverPage(observer)
    history: list[str] = []
    model_calls = 0
    success = False

    await observer.open(task.start["url"])
    await observer.wait_settled()
    for _ in range(max_steps):
        shot = await observer.connection().call("Page.captureScreenshot", format="png")
        w, h = (int(v) for v in observer.window_size.split(","))
        view = {"width": w, "height": h}
        data_uri = "data:image/png;base64," + shot["data"]
        body = {"model": client.model, "max_tokens": 1024,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "slow", "schema": SLOW_SCHEMA}},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": slow_prompt(goal, view["width"], view["height"],
                                                         history)},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ]}]}
        r = client._http.post("/chat/completions", json=body)
        model_calls += 1
        try:
            parsed = json.loads(r.json()["choices"][0]["message"]["content"] or "{}")
        except Exception:
            parsed = {}
        action = parsed.get("action", "done")
        if action == "done":
            success = True
            break
        await slow_act(observer, action, int(parsed.get("x", 0)), int(parsed.get("y", 0)),
                       str(parsed.get("text", "")))
        history.append(f"{action} at ({parsed.get('x')},{parsed.get('y')}) "
                       f"{parsed.get('text', '')}".strip())
        await observer.wait_settled()

    elapsed = time.perf_counter() - started
    checks = await run_checks(task.checks, answer=None, page=page, sandbox=sandbox,
                              started_ts=started)
    return {"mode": "slow", "success": bool(success and all(c.ok for c in checks)),
            "elapsed_s": round(elapsed, 2), "model_calls": model_calls, "steps": len(history)}


# ---------------------------------------------------------------- quiff + replay

async def _loop_run(observer, decider, task, mode: str, rescuer=None) -> dict:
    started = time.perf_counter()
    sandbox = setup_sandbox(task)
    logger = StepLogger(Path("runs") / f"race-{time.strftime('%Y%m%d-%H%M%S')}-{task.id}",
                        task.id)
    loop = FastLoop(observer, decider, logger, rescuer=rescuer)
    result = await loop.run(task.goal, start_url=task.start["url"], subgoal=task.goal)
    from quiff.verify import wait_stable_url

    await wait_stable_url(observer)
    page = ObserverPage(observer)
    checks = await run_checks(task.checks, answer=result.answer, page=page, sandbox=sandbox,
                              started_ts=started)
    if not all(c.ok for c in checks):
        print(f"    [{mode}] check details: " +
              "; ".join(f"{c.check_type}={c.ok}:{c.detail[:60]}" for c in checks))
    model_calls = 0 if mode == "replay" else result.steps
    return {"mode": mode,
            "success": bool(all(c.ok for c in checks)),
            "elapsed_s": round(time.perf_counter() - started, 2),
            "model_calls": model_calls, "steps": result.steps}


async def quiff_run(observer, task) -> dict:
    decider = MuseDecider(effort="minimal")
    rescuer = MuseDecider(effort="high", max_tokens=4096)
    return await _loop_run(observer, decider, task, "quiff", rescuer=rescuer)


async def replay_run(observer, task) -> dict:
    return await _loop_run(observer, ReplayDecider(load_flow(task.id)), task, "replay")


# ---------------------------------------------------------------- main

async def race(runs: int) -> dict:
    load_env()
    task = get_task(TASK_ID, load_tasks())
    client = muse_spark()
    results: list[dict] = []

    observer = BrowserObserver(headless=True)
    try:
        for i in range(runs):
            results.append(await slow_run(client, observer, task))
        for i in range(runs):
            results.append(await quiff_run(observer, task))
        for i in range(runs):
            results.append(await replay_run(observer, task))
    finally:
        await observer.stop()

    summary = {}
    for mode in ("slow", "quiff", "replay"):
        rs = [r for r in results if r["mode"] == mode]
        times = [r["elapsed_s"] for r in rs]
        summary[mode] = {
            "runs": len(rs),
            "successes": sum(1 for r in rs if r["success"]),
            "median_s": statistics.median(times) if times else None,
            "model_calls": [r["model_calls"] for r in rs],
        }
        print(f"{mode:>7}: {summary[mode]['successes']}/{len(rs)} success · "
              f"median {summary[mode]['median_s']} s · model calls "
              f"{summary[mode]['model_calls']}")
    return {"task": TASK_ID, "runs_per_mode": runs, "results": results, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    out = asyncio.run(race(args.runs))
    path = Path("runs") / f"race-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Saved {path}")


if __name__ == "__main__":
    main()

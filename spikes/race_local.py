"""The race on a local, deterministic target: no network noise, no cold starts.

Serves a login page (a stand-in for the-internet.herokuapp.com/login) on
127.0.0.1, then races three modes against it:

    slow    screenshot + one big-model call per step, pixel clicks
    quiff   the action table + minimal Muse Spark decisions
    replay  the recorded flow, zero model calls

    uv run python spikes/race_local.py --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # spikes/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from quiff.models.meta import MuseDecider, load_env
from quiff.tasks import Task, sandbox_dir

LOGIN_PAGE = """<!doctype html><html><head><title>Login Page</title></head>
<body><h2>Login Page</h2><p>This is a local test target.</p>
<form method="post" action="/authenticate">
  <label>Username <input type="text" name="username"></label><br>
  <label>Password <input type="password" name="password"></label><br>
  <button type="submit"> Login</button>
</form></body></html>"""

FAIL_PAGE = """<html><head><title>Login Failed</title></head>
<body><p>Your username is invalid!</p></body></html>"""

SECURE_PAGE = """<html><head><title>Secure Area</title></head>
<body><div class="flash success">You logged into a secure area!</div>
<h2> Secure Area</h2><p>Welcome to the secure area.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the race output clean
        pass

    def _send(self, body: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        body_bytes = body.encode()
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if self.path.startswith("/login"):
            self._send(LOGIN_PAGE)
        elif self.path.startswith("/secure"):
            self._send(SECURE_PAGE)
        else:
            self._send(FAIL_PAGE, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = self.rfile.read(length).decode()
        if "username=tomsmith" in form and "password=SuperSecretPassword%21" in form:
            self.send_response(302)
            self.send_header("Location", "/secure")
            self.send_header("Content-Length", "0")  # keep-alive needs the length
            self.end_headers()
        else:
            self._send(FAIL_PAGE)


def serve() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def local_task() -> Task:
    return Task(
        id="local-login",
        app="chrome",
        split="dev",
        goal="Log in to the local test site with username tomsmith and password SuperSecretPassword!",
        start={"url": "http://127.0.0.1:8765/login"},
        checks=[
            {"type": "url", "pattern": r"/secure$"},
            {"type": "page_text", "contains": "You logged into a secure area!"},
        ],
    )


async def race(runs: int) -> dict:
    load_env()
    server = serve()
    task = local_task()
    results: list[dict] = []
    try:
        from race import quiff_run, replay_run, slow_run
        from quiff.models.meta import muse_spark
        from quiff.observe.browser import BrowserObserver

        client = muse_spark()
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
    finally:
        server.shutdown()
    return results


async def quiff_run_with(observer, decider, task):
    from race import quiff_run
    return await quiff_run(observer, task) if decider is None else await _plain(observer, decider, task)


async def _plain(observer, decider, task):
    from quiff.checks import run_checks
    from quiff.loop import FastLoop
    from quiff.tasks import ObserverPage
    import time as _t
    started = _t.perf_counter()
    loop = FastLoop(observer, decider)
    result = await loop.run(task.goal, start_url=task.start["url"], subgoal=task.goal)
    page = ObserverPage(observer)
    checks = await run_checks(task.checks, answer=result.answer, page=page,
                              sandbox=sandbox_dir(task), started_ts=_t.time())
    return {"mode": "quiff", "success": bool(all(c.ok for c in checks)),
            "elapsed_s": round(_t.perf_counter() - started, 2),
            "model_calls": result.steps, "steps": result.steps}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    results = asyncio.run(race(args.runs))

    summary = {}
    for mode in ("slow", "quiff", "replay"):
        rs = [r for r in results if r["mode"] == mode]
        if not rs:
            continue
        times = [r["elapsed_s"] for r in rs]
        summary[mode] = {
            "runs": len(rs),
            "successes": sum(1 for r in rs if r["success"]),
            "median_s": statistics.median(times),
            "model_calls": [r["model_calls"] for r in rs],
        }
        print(f"{mode:>7}: {summary[mode]['successes']}/{len(rs)} success · "
              f"median {summary[mode]['median_s']} s · model calls "
              f"{summary[mode]['model_calls']}")
    out = Path("runs") / f"race-local-{time.strftime('%Y-%m-%d-%H%M%S')}.json"
    out.write_text(json.dumps({"results": results, "summary": summary}, indent=2),
                   encoding="utf-8")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()

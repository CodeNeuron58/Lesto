"""Measure the browser action table: how fast a page reads and how big it is.

Companion to the UI Automation probe (uia_probe.py). Launches its own Chrome,
opens the URL, then reads the page several times and reports rows, tokens and
read time. Results go to runs/ (gitignored).

Usage:
    uv run python spikes/browser_probe.py --url https://example.com --runs 5
    uv run python spikes/browser_probe.py --url spikes/fixtures/action_table.html --headed --dump
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import statistics
import time
import urllib.parse

from quiff.observe.browser import BrowserObserver, BrowserError


def _file_url(path: str) -> str:
    return pathlib.Path(path).resolve().as_uri()


async def main_async(args: argparse.Namespace) -> None:
    url = args.url
    if not url.startswith(("http://", "https://", "file://")):
        url = _file_url(url)

    observer = BrowserObserver(headless=not args.headed, port=args.port)
    print(f"Opening {url} ({'headed' if args.headed else 'headless'}, port {args.port}) ...")
    try:
        table = await observer.open(url)
        print(f"First read: {len(table.rows)} rows · ~{table.approx_tokens()} tokens · "
              f"{table.read_ms} ms read")
        print(f"Title: {table.title}")

        timings = []
        for run in range(args.runs):
            reread = await observer.read()
            timings.append(reread.read_ms or 0)
            if args.runs > 1:
                await asyncio.sleep(0.3)
        timings.sort()
        summary = {
            "url": url,
            "title": table.title,
            "rows": len(table.rows),
            "approx_tokens": table.approx_tokens(),
            "first_read_ms": table.read_ms,
            "reread_ms": timings,
            "reread_median_ms": statistics.median(timings) if timings else None,
            "headed": args.headed,
            "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        print(f"Re-reads ({args.runs}): {[f'{t} ms' for t in timings]}"
              f" · median {summary['reread_median_ms']} ms")

        out = pathlib.Path("runs") / (
            "browser-probe-" + time.strftime("%Y-%m-%d-%H%M%S") + "-"
            + (urllib.parse.urlparse(url).hostname or "local") + ".json"
        )
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Saved {out}")

        if args.dump:
            print("---")
            print(table.render())
    finally:
        await observer.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://example.com/", help="page to open")
    parser.add_argument("--runs", type=int, default=5, help="number of re-reads to time")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--port", type=int, default=9222, help="remote debugging port")
    parser.add_argument("--dump", action="store_true", help="print the full action table")
    try:
        asyncio.run(main_async(parser.parse_args()))
    except BrowserError as exc:
        raise SystemExit(f"browser probe failed: {exc}")


if __name__ == "__main__":
    main()

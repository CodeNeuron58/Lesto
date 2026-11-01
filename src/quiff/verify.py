"""Stage 5, Verify: plain code checks that the expected change happened.

Waits on UI events instead of fixed sleeps, and stops the same action
repeating on the same screen.
"""

from __future__ import annotations

import asyncio
import inspect
import time

from .observe.browser import BrowserObserver


async def wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.1) -> bool:
    """Poll a plain-code predicate until it holds or the timeout passes.

    Real UI-event waits arrive with the desktop stage; browser pages settle via
    the observer's readyState wait, so polling plain code is honest for now.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(interval)


async def wait_stable_url(observer: BrowserObserver, *, samples: int = 2,
                          interval: float = 0.25, timeout: float = 6.0) -> str:
    """Wait until the page URL stops changing and return it.

    After the last action, a form POST or redirect can still be in flight;
    checking the page before it settles races the navigation. Evidence, not
    a clock: the URL must read the same on `samples` consecutive reads.
    """
    async def url() -> str:
        try:
            return (await observer.target_info()).get("url", "")
        except Exception:
            return ""

    last = await url()
    stable = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        current = await url()
        if current == last:
            stable += 1
            if stable >= samples - 1:
                return current
        else:
            stable = 0
        last = current
    return last


class LoopDetector:
    """Stops the same action firing on the same screen over and over."""

    def __init__(self, limit: int = 3) -> None:
        self._limit = limit
        self._recent: list[tuple[str, str, str]] = []

    def check(self, action: str, ref: str, screen: str) -> bool:
        """Record one step; True when the same action+row+screen hit the limit."""
        self._recent.append((action, ref, screen))
        self._recent[:] = self._recent[-self._limit:]
        if len(self._recent) < self._limit:
            return False
        first = self._recent[0]
        return all(entry == first for entry in self._recent)

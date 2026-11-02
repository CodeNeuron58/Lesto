"""Browser observer: reads Chrome over the DevTools Protocol and builds the action table.

Uses Accessibility.getFullAXTree and DOMSnapshot.captureSnapshot directly.
"""

from __future__ import annotations

import asyncio
import json
import re
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .table import ActionRow, ActionTable


SESSION_DROP_SIGS = ("not attached", "no close frame", "connection closed",
                     "target closed")


def is_session_drop(exc: Exception) -> bool:
    """True for every error signature that means the page session died."""
    text = str(exc).lower()
    return any(sig in text for sig in SESSION_DROP_SIGS)

# Roles a user can act on, mapped to what Quiff may do with them.
ROLE_ACTIONS: dict[str, tuple[str, ...]] = {
    "button": ("click",),
    "link": ("click",),
    "checkbox": ("click",),
    "radio": ("click",),
    "switch": ("click",),
    "tab": ("click",),
    "menuitem": ("click",),
    "menuitemcheckbox": ("click",),
    "menuitemradio": ("click",),
    "treeitem": ("click",),
    "option": ("click",),
    "textbox": ("type",),
    "searchbox": ("type",),
    "combobox": ("click", "select"),
    "slider": ("click", "type"),
    "spinbutton": ("click", "type"),
}
# Roles that stay useful without a name; everything else is dropped when
# unnamed. Bare checkboxes and radios are common and perfectly clickable.
MAX_TABLE_ROWS = 250
"""Cua caps candidates at 24, jev at 250 — beyond this the model wanders."""


CONSENT_LABEL = re.compile(r"\b(accept|agree|consent|ok|allow|got it|reject|decline|deny|manage)\b", re.I)


RISKY_DIALOG = re.compile(r"(delete|remove|confirm|permanently|send|pay)", re.I)


def _collapse_consent_walls(rows: list[ActionRow]) -> list[ActionRow]:
    """jev-ra's consent-wall collapse: while a dialog overlay is up, the only
    sensible actions are its accept-buttons — everything else is noise.

    Safety: the collapse is skipped when the dialog carries destructive words
    (a 'Delete file? OK' confirmation must reach the approval guard with its
    context intact, not be reduced to a bare OK)."""
    has_dialog = any(r.role == "dialog" for r in rows)
    if not has_dialog:
        return rows
    if any(RISKY_DIALOG.search(r.name) for r in rows):
        return rows
    accept = [r for r in rows if r.role in ("button", "link")
              and CONSENT_LABEL.search(r.name)]
    return accept or rows


def _site_of(url_or_host: str) -> str:
    host = re.sub(r"^https?://", "", url_or_host).split("/")[0].split(":")[0]
    parts = host.rsplit(".", 3)
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _demote_off_site(rows: list[ActionRow], current_url: str) -> list[ActionRow]:
    """Links away from the current site sort last — a goal is sometimes
    exactly one of them, so they stay on offer (jev-ra elsewhere())."""
    home = _site_of(current_url)
    if not home:
        return rows
    on_site, away = [], []
    for row in rows:
        match = re.search(r'https?://([^/\s"]+)', row.ref) if row.ref else None
        off = row.role == "link" and bool(match and _site_of(match.group(1)) != home)
        (away if off else on_site).append(row)
    return on_site + away


def _filter_viewport(rows: list[ActionRow], bounds: dict[int, tuple[int, int, int, int]],
                     viewport: tuple[int, int]) -> list[ActionRow]:
    """Keep rows whose bounding box intersects the viewport.

    Elements without known bounds (rare) are kept — truncation does not
    prove absence (Cua skill doc). Rows are kept in table order.
    """
    vw, vh = viewport
    kept: list[ActionRow] = []
    for row in rows:
        target = row.target
        b = bounds.get(target) if isinstance(target, int) else None
        if b is None:
            kept.append(row)
            continue
        x, y, w, h = b
        if w <= 0 or h <= 0:
            continue  # collapsed: not actionable
        if x + w < 0 or y + h < 0 or x > vw or y > vh:
            continue  # entirely outside the viewport
        kept.append(row)
    return kept


UNNAMED_OK = {"textbox", "searchbox", "combobox", "slider", "spinbutton",
              "checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}


class BrowserError(RuntimeError):
    pass


class CDPConnection:
    """A tiny DevTools Protocol client: JSON messages over one WebSocket.

    `call` sends a command and waits for its reply; `next_event` waits for the
    next event of one method. That is all Quiff needs; no third-party CDP layer.
    """

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: dict[str, list[dict]] = {}
        self._event_arrived: asyncio.Condition | None = None
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> "CDPConnection":
        self._event_arrived = asyncio.Condition()
        self._ws = await connect(self._ws_url, max_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _read_loop(self) -> None:
        assert self._ws and self._event_arrived
        async for raw in self._ws:
            message = json.loads(raw)
            if "id" in message:
                future = self._pending.pop(message["id"], None)
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(
                            BrowserError(message["error"].get("message", "DevTools error"))
                        )
                    else:
                        future.set_result(message.get("result", {}))
            elif "method" in message:
                async with self._event_arrived:
                    self._events.setdefault(message["method"], []).append(message.get("params", {}))
                    self._event_arrived.notify_all()

    async def call(self, method: str, timeout: float = 15.0, **params) -> dict:
        assert self._ws
        try:
            return await self._call_inner(method, timeout, **params)
        except WebSocketException as exc:
            # real drops surface as websockets errors, not BrowserError —
            # normalize so the reconnect machinery can see them
            raise BrowserError(str(exc)) from None

    async def _call_inner(self, method: str, timeout: float = 15.0, **params) -> dict:
        assert self._ws
        self._next_id += 1
        call_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        await self._ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(call_id, None)
            raise BrowserError(f"{method} timed out after {timeout}s") from None

    def drain_events(self) -> None:
        """Consume everything buffered. Call before navigating: events still
        queued belong to the page this navigation is leaving (jev-ra)."""
        self._events.clear()

    async def next_event(self, method: str, timeout: float = 20.0) -> dict:
        assert self._event_arrived
        deadline = time.monotonic() + timeout
        while True:
            buffered = self._events.get(method)
            if buffered:
                return buffered.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError(f"no {method} event within {timeout}s")
            async with self._event_arrived:
                try:
                    await asyncio.wait_for(self._event_arrived.wait(), remaining)
                except asyncio.TimeoutError:
                    raise BrowserError(f"no {method} event within {timeout}s") from None


class BrowserObserver:
    """Launches its own Chrome with remote debugging and reads pages into action tables.

    One tab stays connected for the whole run: open() starts it, goto() and read()
    reuse the same DevTools connection.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        port: int = 9222,
        chrome_path: str | None = None,
        window_size: str = "1280,900",
        direct: bool = True,
    ) -> None:
        """direct=True bypasses the system proxy entirely: the machine's proxy
        was intercepting even 127.0.0.1 and intermittently breaking pages."""
        self.headless = headless
        self.direct = direct
        self.port = port
        self.window_size = window_size
        self._chrome = chrome_path or os.environ.get("QUIFF_CHROME") or _find_chrome()
        self._process: subprocess.Popen | None = None
        self._user_data_dir: str | None = None
        self._target_id: str | None = None
        self._cdp: CDPConnection | None = None

    async def open(self, url: str) -> ActionTable:
        """Start Chrome if needed, open a tab at `url`, and read its action table."""
        if self._cdp is None:
            self.start()
            await self._wait_for_debugger()
            target = await self._new_tab(url)
            self._target_id = target["id"]
            self._cdp = await CDPConnection(target["webSocketDebuggerUrl"]).__aenter__()
            await self._cdp.call("Page.enable")
            await self._cdp.call("Accessibility.enable")
        return await self.goto(url)

    async def goto(self, url: str) -> ActionTable:
        """Navigate the current tab and read the new page.

        jev-ra's lesson: never trust protocol events for the load lifecycle —
        a buffered loadEventFired from the PREVIOUS navigation makes this
        return instantly on a half-loaded page. Drain the buffer first, then
        ask the page itself (an in-page load promise dies with the document,
        so a navigation can never satisfy it stale).
        """
        if self._cdp is None:
            raise BrowserError("no open tab; call open() first")
        self._cdp.drain_events()
        try:
            await self._goto_and_settle(url)
            info = await self._target_info()
            return await self._read(title=info.get("title", ""), url=info.get("url", url))
        except BrowserError as exc:
            if not self._is_session_drop(exc):
                raise
            # the page session dropped mid-navigation: re-attach and redo once
            await self.reconnect()
            await self._goto_and_settle(url)
            info = await self._target_info()
            return await self._read(title=info.get("title", ""), url=info.get("url", url))

    async def _goto_and_settle(self, url: str) -> None:
        await self._cdp.call("Page.navigate", url=url)
        await self._wait_page_loaded()
        await self._settle()

    @staticmethod
    def _is_session_drop(exc: Exception) -> bool:
        return is_session_drop(exc)

    async def _wait_page_loaded(self, timeout: float = 30.0) -> None:
        """Ask the page (not the protocol) whether it finished loading.

        The evaluate runs an in-page promise that resolves on the load event
        with a budget fallback. If the document is replaced mid-wait, the
        evaluate fails with a context error and we simply ask again in the
        new document (jev-ra session.py READY_JS pattern).
        """
        assert self._cdp
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = await self._cdp.call(
                    "Runtime.evaluate",
                    expression=(
                        "(budget => new Promise(resolve => {"
                        "  const done = () => resolve(document.readyState);"
                        "  if (document.readyState === 'complete') return done();"
                        "  addEventListener('load', done, {once: true});"
                        "  setTimeout(done, budget);"
                        "}))(4000)"
                    ),
                    awaitPromise=True,
                    returnByValue=True,
                    timeout=8.0,
                )
                if result.get("result", {}).get("value") == "complete":
                    return
            except BrowserError as exc:
                if "context" not in str(exc).lower():
                    await asyncio.sleep(0.2)  # real error: brief pause, keep asking
            await asyncio.sleep(0.2)

    async def read(self) -> ActionTable:
        """Re-read the current page without navigating (fresh table between steps)."""
        if self._cdp is None:
            raise BrowserError("no open tab; call open() first")
        try:
            info = await self._target_info()
            return await self._read(title=info.get("title", ""), url=info.get("url", ""))
        except BrowserError as exc:
            if not self._is_session_drop(exc):
                raise
            await self.reconnect()
            info = await self._target_info()
            return await self._read(title=info.get("title", ""), url=info.get("url", ""))

    async def page_key(self) -> str | None:
        if self._cdp is None:
            raise BrowserError("no open tab; call open() first")
        return await self._page_key()

    def connection(self) -> CDPConnection:
        """The live DevTools connection, for the acting stage's input commands."""
        if self._cdp is None:
            raise BrowserError("no open tab; call open() first")
        return self._cdp

    async def reconnect(self) -> None:
        """Re-attach to the same tab after its session dropped ('Not attached
        to an active page' after many navigations on a warm tab)."""
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=10.0
        ) as response:
            targets = json.loads(response.read())
        target = next((t for t in targets if t.get("id") == self._target_id), None)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise BrowserError("the tab is gone; cannot re-attach")
        await self._cdp.__aexit__(None, None, None)
        self._cdp = await CDPConnection(target["webSocketDebuggerUrl"]).__aenter__()
        await self._cdp.call("Page.enable")
        await self._cdp.call("Accessibility.enable")

    async def call(self, method: str, timeout: float = 15.0, **params) -> dict:
        """One DevTools command with automatic session recovery."""
        if self._cdp is None:
            raise BrowserError("no open tab; call open() first")
        try:
            return await self._cdp.call(method, timeout=timeout, **params)
        except BrowserError as exc:
            text = str(exc).lower()
            if not any(sig in text for sig in (
                "not attached", "no close frame", "connection closed", "target closed",
            )):
                raise
            await self.reconnect()
            return await self._cdp.call(method, timeout=timeout, **params)

    async def wait_settled(self) -> None:
        """Bounded wait for the document to stop churning after an action."""
        if self._cdp is not None:
            await self._settle()

    async def stop(self) -> None:
        if self._cdp:
            await self._cdp.__aexit__(None, None, None)
            self._cdp = None
        self._target_id = None
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        if self._user_data_dir:
            shutil.rmtree(self._user_data_dir, ignore_errors=True)
            self._user_data_dir = None

    def start(self) -> None:
        if self._process:
            return
        self._user_data_dir = tempfile.mkdtemp(prefix="quiff-chrome-")
        flags = [
            self._chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self._user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter,CalculateNativeWinOcclusion",
            # Cua's documented launch flags: background/occluded tabs get
            # renderer-suspended, which breaks synthetic input and reads on
            # warm tabs — the 'click succeeded, nothing happened' failure mode
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--proxy-bypass-list=<-loopback>",  # local test targets skip the system proxy
            "--mute-audio",
            f"--window-size={self.window_size.replace(',', 'x')}",
        ]
        if self.direct:
            flags.append("--no-proxy-server")
        if self.headless:
            flags.append("--headless=new")
        self._process = subprocess.Popen(
            flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    async def _wait_for_debugger(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        url = f"http://127.0.0.1:{self.port}/json/version"
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url, timeout=1.0).read()
                return
            except (urllib.error.URLError, OSError):
                await asyncio.sleep(0.2)
        raise BrowserError(
            f"Chrome did not open its debugger on port {self.port} within {timeout:.0f}s"
        )

    async def _new_tab(self, url: str) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT"
        )
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405):
                raise BrowserError(f"could not open a tab: HTTP {exc.code}") from None
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/new?{url}", timeout=10.0
            ) as response:  # older Chrome only answers on GET
                return json.loads(response.read())
        except urllib.error.URLError as exc:
            raise BrowserError(f"could not reach Chrome's debugger: {exc.reason}") from None

    async def target_info(self) -> dict:
        """The current tab's DevTools entry: id, title, url."""
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=10.0
        ) as response:
            targets = json.loads(response.read())
        for target in targets:
            if target.get("id") == self._target_id:
                return target
        return {}

    async def _target_info(self) -> dict:
        return await self.target_info()

    async def _settle(self) -> None:
        """Wait until the page reports itself ready: load complete and no
        pending resource requests. Evidence-based and fast — the old version
        also required DOM stillness, which animated pages never reach, so
        every step burned the full budget while waiting for a stillness that
        dynamic pages don't have. Bounded at 1.5 s; the page_key guard and
        the wait budget catch anything this returns early on.
        """
        assert self._cdp
        expression = (
            "(async () => {"
            "  const t0 = performance.now();"
            "  while (performance.now() - t0 < 1200) {"
            "    const ready = document.readyState === 'complete';"
            "    const pending = performance.getEntriesByType('resource')"
            "      .filter(r => r.responseEnd === 0 &&"
            "        !/analytics|ads|doubleclick|googlesyndication/.test(r.name) &&"
            "        performance.now() - r.startTime < 10000).length;"
            "    if (ready && pending === 0) return 'ready';"
            "    await new Promise(r => setTimeout(r, 100));"
            "  }"
            "  return 'budget';"
            "})()"
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                await self._cdp.call(
                    "Runtime.evaluate", expression=expression,
                    awaitPromise=True, returnByValue=True, timeout=4.0,
                )
                return  # ready or budget-exhausted
            except BrowserError as exc:
                # a navigation destroyed the page context mid-wait: ask again
                # in the new document, until the deadline
                if "context" not in str(exc).lower():
                    return
                await asyncio.sleep(0.2)

    async def _read(self, *, title: str, url: str) -> ActionTable:
        assert self._cdp
        started = time.perf_counter()
        tree = await self._cdp.call("Accessibility.getFullAXTree", timeout=30.0)
        rows = _table_rows(tree.get("nodes", []))
        bounds, viewport = await self._viewport_bounds()
        rows = _filter_viewport(rows, bounds, viewport)
        rows = _collapse_consent_walls(rows)
        rows = _demote_off_site(rows, url or "")
        omitted = max(0, len(rows) - MAX_TABLE_ROWS)
        rows = rows[:MAX_TABLE_ROWS]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        page_key = await self._page_key()
        return ActionTable(
            title=title or "(untitled page)", rows=tuple(rows), url=url or None,
            read_ms=elapsed_ms, page_key=page_key, omitted=omitted,
        )

    async def _viewport_bounds(self) -> tuple[dict[int, tuple[int, int, int, int]], tuple[int, int]]:
        """backendNodeId -> (x, y, w, h) from one DOMSnapshot, plus the viewport.

        github.com exposes 100+ actionable rows page-wide; only what the
        viewport actually shows should reach the decision model (jev
        snapshot.js hit-tests; Cua caps at 24). One snapshot call, one dict.
        """
        assert self._cdp
        view = {"width": 1280, "height": 900}
        try:
            w, h = (int(v) for v in self.window_size.split(","))
            view = {"width": w, "height": h}
        except (ValueError, AttributeError):
            pass
        try:
            # computedStyles=[] is REQUIRED on current Chrome or the call is
            # rejected with Invalid parameters (probed on Chrome 156)
            snap = await self._cdp.call("DOMSnapshot.captureSnapshot",
                                        computedStyles=[], timeout=15.0)
        except BrowserError:
            return {}, (view["width"], view["height"])
        bounds: dict[int, tuple[int, int, int, int]] = {}
        for doc in snap.get("documents", []):
            layout = doc.get("layout", {})
            node_index = layout.get("nodeIndex", [])
            node_bounds = layout.get("bounds", [])
            backend_ids = (doc.get("nodes", {}) or {}).get("backendNodeId", [])
            # bounds are indexed by layout.nodeIndex, which is NOT the list
            # position of backendNodeId — join through nodeIndex (probed:
            # positional zip misassigned 4 of 5 bounds)
            for i, ni in enumerate(node_index):
                if i < len(node_bounds) and ni < len(backend_ids):
                    b = node_bounds[i]
                    if isinstance(b, list) and len(b) >= 4:
                        bounds[backend_ids[ni]] = (b[0], b[1], b[2], b[3])
        return bounds, (view["width"], view["height"])

    async def _page_key(self) -> str | None:
        """A cheap fingerprint of the page the table was read from.

        timeOrigin changes on every real navigation; href/scroll catch
        same-document changes (jev-ra snapshot.js page_key). The acting stage
        refuses to touch a page whose key no longer matches the observation.
        """
        try:
            result = await self._cdp.call(
                "Runtime.evaluate",
                expression=(
                    "JSON.stringify({t: performance.timeOrigin, h: location.href,"
                    " x: scrollX, y: scrollY})"
                ),
                returnByValue=True,
                timeout=5.0,
            )
            return result.get("result", {}).get("value")
        except BrowserError:
            return None


# Roles that carry no action context of their own; they are walked through and
# kept out of a row's parent path so refs stay readable.
PASS_THROUGH = {"RootWebArea", "WebArea", "none", "InlineTextBox", "StaticText", "LabelText", "generic"}


def _table_rows(nodes: list[dict]) -> list[ActionRow]:
    by_id = {node["nodeId"]: node for node in nodes if "nodeId" in node}
    parent_of: dict[str, str] = {}
    for node in nodes:
        node_id = node.get("nodeId")
        if node_id is None:
            continue
        for child_id in node.get("childIds") or []:
            parent_of[child_id] = node_id
    roots = [node_id for node_id in by_id if node_id not in parent_of]

    rows: list[ActionRow] = []
    visited: set[str] = set()

    def path_ref(path: list[tuple[str, str]]) -> str:
        if not path:
            return "(root)"
        return " < ".join(f"{role}='{name[:20]}'" for role, name in path[-3:])

    def visit(node_id: str, path: list[tuple[str, str]]) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = by_id.get(node_id)
        if node is None:
            return
        role = _role_name(node)
        name = (node.get("name") or {}).get("value", "") or ""
        child_ids = node.get("childIds") or []
        if node.get("ignored"):
            # The node is pruned from the tree, but its children are still real
            # controls; skip only the node itself and keep walking.
            for child_id in child_ids:
                visit(child_id, path)
            return
        here = path if role in PASS_THROUGH else path + [(role, name)]
        actions = ROLE_ACTIONS.get(role)
        if actions and not _is_disabled(node) and (name or role in UNNAMED_OK):
            value = node.get("value") or {}
            text = value.get("value") if isinstance(value, dict) else None
            state = None
            if role in ("checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"):
                checked = _property(node, "checked")
                state = "mixed" if checked == "mixed" else (
                    "checked" if checked and checked != "false" else "unchecked"
                )
            rows.append(
                ActionRow(
                    index=len(rows) + 1,
                    role=role,
                    name=name,
                    actions=actions,
                    enabled=True,
                    value=text if isinstance(text, str) else None,
                    state=state,
                    ref=path_ref(here),
                    target=node.get("backendDOMNodeId"),
                )
            )
        for child_id in child_ids:
            visit(child_id, here)

    for root_id in roots:
        visit(root_id, [])
    return rows


def _role_name(node: dict) -> str:
    """The AX role string lives in role.value ("button"); role.type is a data-type tag."""
    role_field = node.get("role") or {}
    return str(role_field.get("value") or role_field.get("type") or "")


def _property(node: dict, name: str):
    for prop in node.get("properties") or []:
        if prop.get("name") == name:
            value = prop.get("value") or {}
            return value.get("value") if isinstance(value, dict) else value
    return None


def _is_disabled(node: dict) -> bool:
    checked = _property(node, "disabled")
    return bool(checked)


def _find_chrome() -> str:
    candidates = [
        os.environ.get("PROGRAMFILES", r"C:\Program Files")
        + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise BrowserError("Chrome not found; set QUIFF_CHROME to chrome.exe")

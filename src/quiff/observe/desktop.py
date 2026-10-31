"""Windows observer: asks the native UI Automation driver for the focused window's controls.

Never walks the whole accessibility tree; only the focused window and controls a user can act on.

The read is the probe's proven fast path: one cache request brings back only
on-screen, enabled controls that support an action, with all their facts in a
single batch. Acting re-finds the one row's live element by AutomationId
(fallback: name + control type), so reads stay light and actions stay possible.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import comtypes
import comtypes.client

from .table import ActionRow, ActionTable

UIA = comtypes.client.GetModule("UIAutomationCore.dll")
uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

PROPERTIES = [
    UIA.UIA_NamePropertyId,
    UIA.UIA_LocalizedControlTypePropertyId,
    UIA.UIA_AutomationIdPropertyId,
    UIA.UIA_IsEnabledPropertyId,
    UIA.UIA_BoundingRectanglePropertyId,
]
PATTERNS = {
    "invoke": UIA.UIA_IsInvokePatternAvailablePropertyId,
    "value": UIA.UIA_IsValuePatternAvailablePropertyId,
    "toggle": UIA.UIA_IsTogglePatternAvailablePropertyId,
    "expand": UIA.UIA_IsExpandCollapsePatternAvailablePropertyId,
    "select": UIA.UIA_IsSelectionItemPatternAvailablePropertyId,
}
# Patterns map to what the decision model may call the action.
PATTERN_ACTIONS = {
    "invoke": ("click",),
    "value": ("type",),
    "toggle": ("click",),
    "expand": ("click",),
    "select": ("click",),
}
# Roles that stay useful unnamed (text boxes are often unlabeled).
UNNAMED_OK = {"edit", "combobox"}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


class DesktopError(RuntimeError):
    pass


def _process_name(pid: int) -> str:
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _visible_windows() -> list[int]:
    """Every visible top-level window, in z-order (front-most last)."""
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def on_window(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(on_window, 0)
    return found


def _top_windows_of_pid(pid: int) -> list[int]:
    return [hwnd for hwnd in _visible_windows() if _pid_of(hwnd) == pid]


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


class DesktopObserver:
    """Reads one app's windows into action tables and hands rows to the acting stage.

    The read always targets the app's current visible window, so when a dialog
    opens (Save As, confirmations) the table becomes the dialog's controls.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None
        self._pid: int | None = None
        self._hwnd: int | None = None
        self._baseline: set[int] = set()
        self._launched = False

    async def open(self, argv: list[str], *, cwd: str | None = None) -> ActionTable:
        """Start a program, wait for its window, and read it.

        Store apps (Notepad on Windows 11) launch through a stub process that
        exits at once, so we attach to whatever window newly appears instead of
        trusting the child process id. The baseline also keeps the observer on
        OUR windows only: the app's process may serve the user's windows too.
        `cwd` also becomes the save dialogs' default folder, keeping test saves
        out of whatever directory Quiff itself was started from.
        """
        if self._process is None:
            before = set(_visible_windows())
            self._baseline = before
            self._launched = True
            self._process = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self._hwnd = await self._wait_for_new_window(before)
            self._pid = _pid_of(self._hwnd)
            user32.SetForegroundWindow(self._hwnd)
            # Warm-up: WinUI and Chromium-based apps build their tree lazily; ask
            # once, give it a beat, and the real read gets the full picture.
            self._read_window(self._hwnd)
            await asyncio.sleep(0.6)
        return await self.read()

    def attach_to_foreground(self) -> None:
        """Watch whatever window the user has in front (the probe's flow)."""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise DesktopError("no foreground window")
        self._hwnd = hwnd
        self._pid = _pid_of(hwnd)
        self._baseline = set(_visible_windows()) - {hwnd}

    async def read(self) -> ActionTable:
        if self._pid is None:
            raise DesktopError("no app attached; call open() or attach_to_foreground() first")
        hwnd = self._current_window()
        if hwnd != self._hwnd:
            self._hwnd = hwnd  # a dialog opened; follow the app's front window
        started = time.perf_counter()
        rows = self._read_window(hwnd)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ActionTable(
            title=_window_title(hwnd) or "(untitled window)",
            rows=tuple(rows),
            read_ms=elapsed_ms,
        )

    async def wait_settled(self) -> None:
        # Dialogs (Save As, prompts) take a moment to build their trees; give
        # them a beat so the next read sees the dialog, not the old window.
        await asyncio.sleep(1.0)

    def current_hwnd(self) -> int | None:
        try:
            return self._current_window()
        except DesktopError:
            return None

    async def stop(self) -> None:
        """Close the windows Quiff launched, discard unsaved test text, and clean up.

        Windows the observer merely attached to are never closed. Store apps launch
        through a stub that exits at once, so terminating the child process is not
        enough — the windows themselves get a close message, and any save prompt
        is answered with Don't save.
        """
        if self._launched and self._pid:
            for hwnd in [w for w in _top_windows_of_pid(self._pid) if w not in self._baseline]:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                # re-scan: prompts and confirm dialogs can appear in cascade
                ours = [
                    w
                    for w in _top_windows_of_pid(self._pid)
                    if w not in self._baseline and user32.IsWindow(w)
                ]
                if not ours:
                    break
                for hwnd in ours:
                    self._answer_dont_save(hwnd)
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._pid = None
        self._hwnd = None
        self._launched = False

    def _answer_dont_save(self, hwnd: int) -> None:
        """Click Don't save / No on a prompt of ours, whether it is a separate
        dialog (Confirm Save As) or drawn inside the window."""
        try:
            element = uia.ElementFromHandle(hwnd)
            window_title = _window_title(hwnd).lower()
            buttons = element.FindAll(
                UIA.TreeScope_Descendants,
                uia.CreatePropertyCondition(
                    UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId
                ),
            )
        except Exception:
            return
        wanted = {"don't save", "dont save", "close without saving"}
        if "confirm save as" in window_title:
            wanted.add("no")  # our junk must not overwrite anything
        for i in range(buttons.Length):
            button = buttons.GetElement(i)
            if (button.CurrentName or "").strip().lower().replace("`", "'") in wanted:
                try:
                    pattern = button.GetCurrentPattern(UIA.UIA_InvokePatternId)
                    if pattern:
                        pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                except Exception:
                    pass
                return

    # ------------------------------------------------------------------ internals

    def _current_window(self) -> int:
        """The front-most window of ours: one the app opened after our baseline."""
        assert self._pid
        ours = [
            hwnd
            for hwnd in _top_windows_of_pid(self._pid)
            if hwnd not in self._baseline
        ]
        if not ours:
            raise DesktopError(f"the app's window is gone (pid {self._pid})")
        front = user32.GetForegroundWindow()
        if front in ours:
            return front
        return ours[-1]

    async def _wait_for_new_window(self, before: set[int], timeout: float = 20.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            titled = [
                hwnd
                for hwnd in _visible_windows()
                if hwnd not in before and _window_title(hwnd).strip()
            ]
            if titled:
                return titled[-1]
            await asyncio.sleep(0.3)
        raise DesktopError(f"no new window appeared within {timeout:.0f}s after launching")

    def _read_window(self, hwnd: int) -> list[ActionRow]:
        window = uia.ElementFromHandle(hwnd)
        cache = uia.CreateCacheRequest()
        for property_id in PROPERTIES + list(PATTERNS.values()):
            cache.AddProperty(property_id)
        cache.AutomationElementMode = UIA.AutomationElementMode_None

        any_pattern = None
        for pattern_property in PATTERNS.values():
            has_it = uia.CreatePropertyCondition(pattern_property, True)
            any_pattern = has_it if any_pattern is None else uia.CreateOrCondition(any_pattern, has_it)
        onscreen = uia.CreatePropertyCondition(UIA.UIA_IsOffscreenPropertyId, False)
        enabled = uia.CreatePropertyCondition(UIA.UIA_IsEnabledPropertyId, True)
        condition = uia.CreateAndCondition(onscreen, uia.CreateAndCondition(enabled, any_pattern))

        elements = window.FindAllBuildCache(UIA.TreeScope_Descendants, condition, cache)
        rows: list[ActionRow] = []
        for i in range(elements.Length):
            element = elements.GetElement(i)
            name = (element.CachedName or "").strip()
            role = (element.CachedLocalizedControlType or "?").lower()
            patterns = [p for p, pid in PATTERNS.items() if element.GetCachedPropertyValue(pid)]
            actions = tuple(dict.fromkeys(a for p in patterns for a in PATTERN_ACTIONS[p]))
            if not actions or (not name and role not in UNNAMED_OK):
                continue
            rows.append(
                ActionRow(
                    index=len(rows) + 1,
                    role=role,
                    name=name,
                    actions=actions,
                    enabled=True,
                    ref=f"{role}='{name[:24]}'",
                    target={
                        "automation_id": element.CachedAutomationId or "",
                        "name": name,
                        "role": role,
                    },
                )
            )
        return rows

    def find_row_element(self, target: dict):
        """Re-find one row's live element for acting: by AutomationId, else name + type."""
        if self._hwnd is None:
            raise DesktopError("no window attached")
        window = uia.ElementFromHandle(self._hwnd)
        automation_id = target.get("automation_id") or ""
        if automation_id:
            condition = uia.CreatePropertyCondition(UIA.UIA_AutomationIdPropertyId, automation_id)
            element = window.FindFirst(UIA.TreeScope_Descendants, condition)
            if element:
                return element
        name_cond = uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, target.get("name") or "")
        role_cond = uia.CreatePropertyCondition(
            UIA.UIA_LocalizedControlTypePropertyId, target.get("role") or ""
        )
        return window.FindFirst(
            UIA.TreeScope_Descendants, uia.CreateAndCondition(name_cond, role_cond)
        )

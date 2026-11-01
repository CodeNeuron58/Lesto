"""Stage 4, Act: perform the chosen action.

Prefer accessibility actions (Invoke, SetValue) on Windows and DevTools input in the
browser; real mouse and keyboard input is the fallback. Anything irreversible
(send, pay, delete, submit) asks the user first.
"""

from __future__ import annotations

import asyncio
import ctypes
import time
from pathlib import Path
from dataclasses import dataclass

from .decide import Decision
from .observe.browser import BrowserError, BrowserObserver
from .observe.table import ActionRow

# Words that make an action irreversible; the user must approve before it runs.
RISKY_WORDS = ("send", "pay", "submit", "delete", "post", "approve", "transfer", "confirm",
               "place order", "checkout")
# Keyboard shortcuts that trigger the same words (e.g. Ctrl+Enter sends in Outlook).
RISKY_KEYS = ("ctrl+enter", "ctrl+d", "shift+delete")


@dataclass
class ActResult:
    ok: bool
    detail: str
    asked_for_approval: bool = False
    ms: int = 0


def needs_approval(decision: Decision, table: ActionTable) -> str | None:
    """Why this decision needs a human's approval, or None if it is routine."""
    if decision.action == "download":
        # the model chose where to write on disk — always a human decision
        return "download to " + (" ".join(decision.values) or "an unspecified path")
    if decision.action == "press":
        key = "+".join(part.strip() for part in
                       " ".join(decision.values).split("+") if part.strip()).lower()
        if any(key.endswith(risky) for risky in RISKY_KEYS):
            return f"keyboard shortcut {key}"
        return None
    if decision.row is None:
        return None
    row = table.by_index(decision.row)
    if row is None:
        return None
    label = f"{row.role} {row.name}".lower()
    if any(risky in label for risky in RISKY_WORDS):
        return f"{row.role} \"{row.name}\""
    return None


async def act(observer, table: ActionTable, decision: Decision) -> ActResult:
    """Perform the decision's action on the page; never raises, reports via ActResult."""
    started = time.perf_counter()
    try:
        if hasattr(observer, "connection"):  # browser observer
            result = await _act_browser(observer, table, decision)
        else:  # desktop observer
            result = _act_desktop(observer, table, decision)
    except Exception as exc:
        result = ActResult(ok=False, detail=f"error: {exc}")
    result.ms = int((time.perf_counter() - started) * 1000)
    return result


# --------------------------------------------------------------------- browser

class _Recovering:
    """Routes CDP calls through the observer so a dropped page session
    ('Not attached to an active page' on long warm tabs) reconnects to the
    same tab and retries once, instead of failing the act."""

    def __init__(self, observer):
        self._observer = observer

    @staticmethod
    def _is_session_drop(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(sig in text for sig in (
            "not attached", "no close frame", "connection closed", "target closed",
        ))

    async def call(self, method: str, timeout: float = 15.0, **params):
        try:
            return await self._observer.connection().call(method, timeout=timeout, **params)
        except BrowserError as exc:
            if not self._is_session_drop(exc):
                raise
            await self._observer.reconnect()
            return await self._observer.connection().call(method, timeout=timeout, **params)


async def _act_browser(observer: BrowserObserver, table: ActionTable, decision: Decision) -> ActResult:
    cdp = _Recovering(observer)

    if table.page_key:
        live = await observer.page_key()
        if live is not None and live != table.page_key:
            return ActResult(ok=False, detail="page changed since the last observation; re-observe")

    if decision.action == "press":
        combo = " ".join(decision.values).strip() or "Enter"
        before_url = (await observer.target_info()).get("url", "")
        before_title = (await observer.target_info()).get("title", "")
        await _press_key(cdp, combo)
        await asyncio.sleep(0.6)
        after_url = (await observer.target_info()).get("url", "")
        after_title = (await observer.target_info()).get("title", "")
        if after_url != before_url:
            return ActResult(ok=True,
                             detail=f"pressed {combo} — page navigated to {after_url}")
        if after_title != before_title:
            return ActResult(ok=True,
                             detail=f"pressed {combo} — page is now '{after_title}'")
        return ActResult(ok=True,
                         detail=f"pressed {combo} — no visible change (URL and title unchanged)")

    if decision.action in ("done", "answer"):
        return ActResult(ok=True, detail=decision.action)

    row = table.by_index(decision.row) if decision.row is not None else None
    if row is None:
        return ActResult(ok=False, detail=f"row {decision.row} not in the table")
    if row.target is None:
        return ActResult(ok=False, detail=f"row {decision.row} has no DOM node to act on")

    if decision.action == "click":
        point = await _center(cdp, row)
        if point is None:
            return ActResult(ok=False, detail=f"row {decision.row} has no box on screen")
        x, y = point
        # mouseMoved first: Chrome routes the press to the node under its
        # hover point (jev-ra and Browser Use both send the move)
        await cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for verb, params in (
            ("Input.dispatchMouseEvent",
             dict(type="mousePressed", x=x, y=y, button="left", clickCount=1)),
            ("Input.dispatchMouseEvent",
             dict(type="mouseReleased", x=x, y=y, button="left", clickCount=1)),
        ):
            await cdp.call(verb, **params)
        return ActResult(ok=True, detail=f"clicked [{decision.row}] {row.role} \"{row.name}\"")

    if decision.action == "type":
        text = " ".join(decision.values)
        await _focus(cdp, row)
        await _select_all(cdp)
        if text:
            await cdp.call("Input.insertText", text=text)
        # verify by reading the field back; if the text didn't land (focus
        # missed, warm-tab quirk), set it through JS with real events so the
        # page sees it — jev-ra/Browser Use input-readback pattern.
        if await _input_value(cdp, row) != text:
            await _focus(cdp, row)
            await _js_set_value(cdp, row, text)
            if await _input_value(cdp, row) != text:
                return ActResult(ok=False, detail=f"text did not land in [{decision.row}] {row.name!r}")
        return ActResult(ok=True, detail=f"typed into [{decision.row}] {row.role} \"{row.name}\"")

    if decision.action == "download":
        url = decision.values[0] if decision.values else ""
        dest = Path(decision.values[1]).expanduser() if len(decision.values) > 1 else None
        if not url or dest is None:
            return ActResult(ok=False, detail="download needs a URL and a destination path")
        import urllib.request

        dest.parent.mkdir(parents=True, exist_ok=True)

        def _fetch() -> bytes:
            with urllib.request.urlopen(url, timeout=60.0) as response:
                return response.read()

        body = await asyncio.to_thread(_fetch)  # keep the event loop free
        with open(dest, "wb") as out:
            out.write(body)
        return ActResult(ok=True, detail=f"downloaded {url} -> {dest}")

    if decision.action == "navigate":
        url = " ".join(decision.values).strip()
        if not url:
            return ActResult(ok=False, detail="navigate without a URL")
        await observer.goto(url)
        return ActResult(ok=True, detail=f"navigated to {url}")

    if decision.action == "select":
        option = " ".join(decision.values)
        matched = await _select_option(cdp, row, option)
        return ActResult(
            ok=matched,
            detail=(f"selected \"{option}\" in [{decision.row}]"
                    if matched else f"option \"{option}\" not found in [{decision.row}]"),
        )

    return ActResult(ok=False, detail=f"unknown action {decision.action!r}")


async def _center(cdp, row: ActionRow) -> tuple[float, float] | None:
    """Scroll the row into view and return its centre point, or None if it has no box."""
    resolved = await cdp.call("DOM.resolveNode", backendNodeId=row.target)
    object_id = (resolved.get("object") or {}).get("objectId")
    if not object_id:
        return None
    await cdp.call(
        "Runtime.callFunctionOn",
        objectId=object_id,
        functionDeclaration="function () { this.scrollIntoView({ block: 'center' }); }",
    )
    box = await cdp.call(
        "Runtime.callFunctionOn",
        objectId=object_id,
        functionDeclaration=(
            "function () { const r = this.getBoundingClientRect();"
            " return { x: r.x + r.width / 2, y: r.y + r.height / 2,"
            " w: r.width, h: r.height }; }"
        ),
        returnByValue=True,
    )
    value = box.get("result", {}).get("value") or {}
    if not value.get("w") or not value.get("h"):
        return None
    return float(value["x"]), float(value["y"])


async def _focus(cdp, row: ActionRow) -> None:
    resolved = await cdp.call("DOM.resolveNode", backendNodeId=row.target)
    object_id = (resolved.get("object") or {}).get("objectId")
    if object_id:
        await cdp.call(
            "Runtime.callFunctionOn",
            objectId=object_id,
            functionDeclaration="function () { this.focus(); }",
        )


async def _select_all(cdp) -> None:
    for phase, params in (
        ("rawKeyDown", dict(type="rawKeyDown", modifiers=2, key="Control",
                            windowsVirtualKeyCode=17, code="ControlLeft")),
        ("keyUp", dict(type="keyUp", modifiers=2, key="Control",
                       windowsVirtualKeyCode=17, code="ControlLeft")),
    ):
        await cdp.call("Input.dispatchKeyEvent", **params)
    for phase, params in (
        ("rawKeyDown", dict(type="rawKeyDown", modifiers=2, key="a", text="a",
                            windowsVirtualKeyCode=65, code="KeyA")),
        ("keyUp", dict(type="keyUp", modifiers=2, key="a",
                       windowsVirtualKeyCode=65, code="KeyA")),
    ):
        await cdp.call("Input.dispatchKeyEvent", **params)


CDP_MODIFIERS = {"ctrl": 2, "alt": 1, "meta": 4, "shift": 8}


async def _press_key(cdp, combo: str) -> None:
    """Press e.g. 'Enter' or 'ctrl+s' — modifiers were silently dropped
    before, which made the approval guard approve a different action."""
    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    modifiers = 0
    modifier_keys = []
    for part in parts:
        if part in CDP_MODIFIERS:
            modifiers |= CDP_MODIFIERS[part]
            modifier_keys.append(part)
    keys = [k for k in parts if k not in CDP_MODIFIERS] or ["Enter"]
    for mk in modifier_keys:
        code, vk, text = _key_spec(mk)
        await cdp.call("Input.dispatchKeyEvent", type="rawKeyDown", key=mk,
                       windowsVirtualKeyCode=vk, code=code, modifiers=modifiers)
    for key in keys:
        code, vk, text = _key_spec(key)
        await cdp.call("Input.dispatchKeyEvent", type="rawKeyDown", key=key,
                       windowsVirtualKeyCode=vk, code=code, text=text,
                       modifiers=modifiers)
        if text:
            # a separate char event with the transition bits: typeahead and
            # form-submit handlers key off the committed character
            await cdp.call("Input.dispatchKeyEvent", type="char", key=key,
                           windowsVirtualKeyCode=vk, code=code, text=text,
                           modifiers=modifiers)
        await cdp.call("Input.dispatchKeyEvent", type="keyUp", key=key,
                       windowsVirtualKeyCode=vk, code=code, modifiers=modifiers)
    for mk in reversed(modifier_keys):
        code, vk, _ = _key_spec(mk)
        await cdp.call("Input.dispatchKeyEvent", type="keyUp", key=mk,
                       windowsVirtualKeyCode=vk, code=code, modifiers=modifiers)


def _key_spec(key: str) -> tuple[str, int, str]:
    specs = {
        "enter": ("Enter", 13, "\r"),
        "tab": ("Tab", 9, ""),
        "escape": ("Escape", 27, ""),
        "backspace": ("Backspace", 8, ""),
        "arrowdown": ("ArrowDown", 40, ""),
        "arrowup": ("ArrowUp", 38, ""),
        "arrowleft": ("ArrowLeft", 37, ""),
        "arrowright": ("ArrowRight", 39, ""),
        "pagedown": ("PageDown", 34, ""),
        "pageup": ("PageUp", 33, ""),
        "delete": ("Delete", 46, ""),
        **{f"f{i}": (f"F{i}", 0x70 + i - 1, "") for i in range(1, 13)},
    }
    return specs.get(key, (key.capitalize(), ord(key.upper()[:1]) if key else 0, key[:1]))


async def _input_value(cdp, row: ActionRow) -> str | None:
    resolved = await cdp.call("DOM.resolveNode", backendNodeId=row.target)
    object_id = (resolved.get("object") or {}).get("objectId")
    if not object_id:
        return None
    try:
        probe = await cdp.call(
            "Runtime.callFunctionOn", objectId=object_id,
            functionDeclaration="function () { return this.value; }", returnByValue=True,
        )
        return probe.get("result", {}).get("value")
    except BrowserError:
        return None


async def _js_set_value(cdp, row: ActionRow, text: str) -> None:
    resolved = await cdp.call("DOM.resolveNode", backendNodeId=row.target)
    object_id = (resolved.get("object") or {}).get("objectId")
    if not object_id:
        return
    await cdp.call(
        "Runtime.callFunctionOn", objectId=object_id,
        functionDeclaration=(
            "function (t) { this.value = t;"
            " this.dispatchEvent(new Event('input', { bubbles: true }));"
            " this.dispatchEvent(new Event('change', { bubbles: true })); }"
        ),
        arguments=[{"value": text}], returnByValue=True,
    )


async def _select_option(cdp, row: ActionRow, option: str) -> bool:
    """Pick a <select> option by its visible text and fire the change events."""
    resolved = await cdp.call("DOM.resolveNode", backendNodeId=row.target)
    object_id = (resolved.get("object") or {}).get("objectId")
    if not object_id:
        return False
    result = await cdp.call(
        "Runtime.callFunctionOn",
        objectId=object_id,
        functionDeclaration=(
            "function (label) {"
            "  if (!this.options) return false;"
            "  const wanted = label.trim().toLowerCase();"
            "  for (const opt of this.options) {"
            "    if (opt.textContent.trim().toLowerCase() === wanted) {"
            "      this.value = opt.value;"
            "      this.dispatchEvent(new Event('input', { bubbles: true }));"
            "      this.dispatchEvent(new Event('change', { bubbles: true }));"
            "      return true;"
            "    }"
            "  }"
            "  return false;"
            " }"
        ),
        arguments=[{"value": option}],
        returnByValue=True,
    )
    return bool(result.get("result", {}).get("value"))


# --------------------------------------------------------------------- desktop

# Virtual-key codes for the keys Quiff may press.
VK_CODES = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08, "space": 0x20,
    "arrowup": 0x26, "arrowdown": 0x28, "arrowleft": 0x25, "arrowright": 0x27,
    "pageup": 0x21, "pagedown": 0x22, "home": 0x24, "end": 0x23,
    "delete": 0x2E, "insert": 0x2D,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 13)},  # F1..F12 = 0x70..0x7B
}
VK_MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


def _act_desktop(observer, table: ActionTable, decision: Decision) -> ActResult:
    """Accessibility patterns first (Invoke, SetValue...); the keyboard is the fallback."""
    if decision.action == "press":
        combo = " ".join(decision.values).strip() or "enter"
        before = observer.current_hwnd()
        _press_combo(observer, combo)
        time.sleep(0.4)
        after = observer.current_hwnd()
        if after != before:
            from .observe.desktop import _window_title
            return ActResult(ok=True,
                             detail=f"pressed {combo} — a new window opened "
                                    f"('{_window_title(after)}')")
        return ActResult(ok=True, detail=f"pressed {combo}")

    if decision.action in ("done", "answer", "wait"):
        return ActResult(ok=True, detail=decision.action)

    if decision.action == "save_as":
        path = " ".join(decision.values)
        return _save_as(observer, path)

    row = table.by_index(decision.row) if decision.row is not None else None
    if row is None:
        return ActResult(ok=False, detail=f"row {decision.row} not in the table")
    element = observer.find_row_element(row.target)
    if element is None:
        return ActResult(ok=False, detail=f"row {decision.row} vanished from the window")

    if decision.action == "click":
        for name, verb in (("Invoke", "invoked"), ("Toggle", "toggled"),
                           ("SelectionItem", "selected"), ("ExpandCollapse", "expanded")):
            pattern = _pattern(element, name)
            if pattern is None:
                continue
            try:
                if name == "Invoke":
                    pattern.Invoke()
                elif name == "Toggle":
                    pattern.Toggle()
                elif name == "SelectionItem":
                    pattern.Select()
                else:
                    pattern.Expand()
                return ActResult(ok=True, detail=f"{verb} [{decision.row}] {row.role} \"{row.name}\"")
            except Exception:
                continue
        return ActResult(ok=False, detail=f"no working pattern on [{decision.row}] {row.name!r}")

    if decision.action == "type":
        text = " ".join(decision.values)
        value = _pattern(element, "Value")
        if value is not None:
            for _ in range(2):
                try:
                    value.SetValue(text)
                    time.sleep(0.1)
                    if _normalized(_read_value(value)) == " ".join(text.split()):
                        return ActResult(
                            ok=True,
                            detail=f"set value of [{decision.row}] {row.role} \"{row.name}\"",
                        )
                except Exception:
                    pass
        element.SetFocus()
        _force_foreground(observer)
        _select_all_desktop()
        _type_text(observer, text)
        return ActResult(ok=True, detail=f"typed into [{decision.row}] {row.role} \"{row.name}\"")

    return ActResult(ok=False, detail=f"unknown action {decision.action!r}")


def _read_value(pattern) -> str | None:
    """Read a Value pattern's current text; comtypes exposes it as .value on
    some interfaces and CurrentValue on others."""
    for attr in ("CurrentValue", "value"):
        try:
            current = getattr(pattern, attr)
        except AttributeError:
            continue
        return current if isinstance(current, str) else None
    try:
        return pattern.GetCurrentValue()
    except Exception:
        return None


def _normalized(text: str | None) -> str:
    return " ".join((text or "").split())


def _pattern(element, name: str):
    """The pattern object for one capability, cast to its real interface.

    GetCurrentPattern returns IUnknown; comtypes needs an explicit
    QueryInterface before Invoke/SetValue work. None when unsupported.
    """
    from .observe.desktop import UIA

    unknown = element.GetCurrentPattern(getattr(UIA, f"UIA_{name}PatternId"))
    if unknown is None:
        return None
    try:
        return unknown.QueryInterface(getattr(UIA, f"IUIAutomation{name}Pattern"))
    except Exception:
        return None


def _press_combo(observer, combo: str) -> None:
    """Press e.g. 'ctrl+s' or 'enter' on the app's foreground window."""
    from .observe.desktop import user32

    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    modifiers = [VK_MODIFIERS[p] for p in parts if p in VK_MODIFIERS]
    keys = [p for p in parts if p not in VK_MODIFIERS]
    _force_foreground(observer)
    for vk in modifiers:
        _send_key(vk)
    for key in keys:
        vk = VK_CODES.get(key) or (ord(key.upper()[:1]) if key else 0)
        _send_key(vk)
        _send_key(vk, keyup=True)
    for vk in reversed(modifiers):
        _send_key(vk, keyup=True)


def _force_foreground(observer) -> None:
    """Put the app's window in front before keyboard events.

    Windows refuses SetForegroundWindow from a background process; tapping Alt
    first is the standard unlock. Without this, a keystroke can land on
    whatever window the user actually had in front.
    """
    from .observe.desktop import user32

    hwnd = observer.current_hwnd()
    if not hwnd:
        return
    for _ in range(3):
        if user32.GetForegroundWindow() == hwnd:
            return
        user32.keybd_event(0x12, 0, 0, 0)  # Alt down
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
        time.sleep(0.2)


def _type_text(observer, text: str) -> None:
    for char in text:
        _send_unicode(char)


def _select_all_desktop() -> None:
    """Ctrl+A so typing replaces whatever the control already held."""
    _send_key(VK_MODIFIERS["ctrl"])
    _send_key(ord("A"))
    _send_key(ord("A"), keyup=True)
    _send_key(VK_MODIFIERS["ctrl"], keyup=True)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                ("dwExtraInfo", ctypes.c_void_p)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint), ("u", _U)]


def _send_input(input_struct: _INPUT) -> None:
    import ctypes as ct

    ct.windll.user32.SendInput(1, ct.byref(input_struct), ct.sizeof(_INPUT))


def _send_key(vk: int, keyup: bool = False) -> None:
    item = _INPUT(type=INPUT_KEYBOARD)
    item.ki = _KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if keyup else 0)
    _send_input(item)


def _send_unicode(char: str) -> None:
    units = [ord(char)]
    if 0x10000 <= ord(char) <= 0x10FFFF:  # astral plane: send the UTF-16 pair
        code = ord(char) - 0x10000
        units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    for unit in units:
        item = _INPUT(type=INPUT_KEYBOARD)
        item.ki = _KEYBDINPUT(wScan=unit, dwFlags=KEYEVENTF_UNICODE)
        _send_input(item)
        item.ki = _KEYBDINPUT(wScan=unit, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        _send_input(item)
    time.sleep(0.025)  # without a beat, Windows drops rapid unicode events


def _save_as(observer, file_path: str) -> ActResult:
    """Save the front document to an exact path: File > Save as, full path, Enter.

    The whole flow lives in the driver (Cua-style) because every app does it
    the same way; the model only names the destination.
    """
    from .observe.desktop import DesktopError

    if not observer.invoke_menu(["File", "Save as"]):
        # fall back to the keyboard shortcut for apps without a File menu
        _press_combo(observer, "ctrl+shift+s")
    dialog = observer.wait_for_window(title_contains="save as", timeout=10.0, owned=True)
    if not dialog:
        # Some apps save silently when the file already has a path; verify by
        # checking the file appears.
        time.sleep(1.0)
        if Path(file_path).exists():
            return ActResult(ok=True, detail=f"saved (no dialog): {file_path}")
        return ActResult(ok=False, detail="Save as dialog never appeared")

    previous = observer._hwnd  # noqa: SLF001 - same package
    observer._hwnd = dialog
    try:
        table_rows = observer._read_window(dialog)
        from .observe.table import ActionTable

        dialog_table = ActionTable(title="Save as", rows=tuple(table_rows))
        edit = next((r for r in table_rows if r.role == "edit"
                     and "file name" in r.name.lower()), None)
        if edit is None:
            return ActResult(ok=False, detail="no File name box in the Save dialog")
        # The common-dialog file box accepts SetValue (and even reads it back)
        # without committing it — only real keyboard events register here.
        el = observer.find_row_element(edit.target)
        if el is None:
            return ActResult(ok=False, detail="File name box vanished")
        try:
            el.SetFocus()
        except Exception:
            pass
        _force_foreground(observer)
        _select_all_desktop()
        _type_text(observer, file_path)
        _press_combo(observer, "enter")
        time.sleep(1.0)
        # The target may already exist: Confirm Save As asks to replace. Our
        # intent was to write this exact file, so answer Yes ourselves.
        confirm = observer.wait_for_window(title_contains="confirm save as",
                                           timeout=3.0, owned=True)
        if confirm:
            _click_button(observer, confirm, "Yes")
            time.sleep(0.8)
    finally:
        observer._hwnd = previous
    # the flush can lag the Enter: poll for the file instead of one check
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if Path(file_path).exists():
            return ActResult(ok=True, detail=f"saved {file_path}")
        confirm = observer.wait_for_window(title_contains="confirm save as",
                                           timeout=0.0, owned=True)
        if confirm:
            from quiff.act import _click_button
            _click_button(observer, confirm, "Yes")
        time.sleep(0.3)
    return ActResult(ok=False, detail=f"save did not land at {file_path}")


def _click_button(observer, hwnd: int, label: str) -> bool:
    """Invoke one named button in a window of ours (Yes/OK/Don't save...)."""
    from .observe.desktop import uia, UIA

    try:
        element = uia.ElementFromHandle(hwnd)
        buttons = element.FindAll(
            UIA.TreeScope_Descendants,
            uia.CreatePropertyCondition(
                UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId
            ),
        )
    except Exception:
        return False
    for i in range(buttons.Length):
        button = buttons.GetElement(i)
        if (button.CurrentName or "").strip().lower() == label.lower():
            try:
                pattern = button.GetCurrentPattern(UIA.UIA_InvokePatternId)
                if pattern:
                    pattern.QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()
                    return True
            except Exception:
                return False
    return False

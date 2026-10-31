"""Stage 4, Act: perform the chosen action.

Prefer accessibility actions (Invoke, SetValue) on Windows and DevTools input in the
browser; real mouse and keyboard input is the fallback. Anything irreversible
(send, pay, delete, submit) asks the user first.
"""

from __future__ import annotations

import ctypes
import time
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
    if decision.action == "press":
        key = " ".join(decision.values).lower()
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

async def _act_browser(observer: BrowserObserver, table: ActionTable, decision: Decision) -> ActResult:
    cdp = observer.connection()

    if decision.action == "press":
        key = " ".join(decision.values).strip() or "Enter"
        await _press_key(cdp, key)
        return ActResult(ok=True, detail=f"pressed {key}")

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
        return ActResult(ok=True, detail=f"typed into [{decision.row}] {row.role} \"{row.name}\"")

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


async def _press_key(cdp, key: str) -> None:
    key = key.strip().strip("+").split("+")[-1].lower()
    code, vk, text = _key_spec(key)
    await cdp.call(
        "Input.dispatchKeyEvent",
        type="rawKeyDown", key=key, windowsVirtualKeyCode=vk, code=code, text=text,
    )
    await cdp.call(
        "Input.dispatchKeyEvent",
        type="keyUp", key=key, windowsVirtualKeyCode=vk, code=code,
    )


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
    }
    return specs.get(key, (key.capitalize(), ord(key.upper()[:1]) if key else 0, key[:1]))


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
}
VK_MODIFIERS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1


def _act_desktop(observer, table: ActionTable, decision: Decision) -> ActResult:
    """Accessibility patterns first (Invoke, SetValue...); the keyboard is the fallback."""
    if decision.action == "press":
        combo = " ".join(decision.values).strip() or "enter"
        _press_combo(observer, combo)
        return ActResult(ok=True, detail=f"pressed {combo}")

    if decision.action in ("done", "answer"):
        return ActResult(ok=True, detail=decision.action)

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
            try:
                value.SetValue(text)
                # Some controls (common-dialog file boxes) accept SetValue but
                # ignore it; verify, or the dialog saves under the old name.
                if (value.Value or "") == text:
                    return ActResult(ok=True, detail=f"set value of [{decision.row}] {row.role} \"{row.name}\"")
            except Exception:
                pass
        element.SetFocus()
        _select_all_desktop()
        _type_text(observer, text)
        return ActResult(ok=True, detail=f"typed into [{decision.row}] {row.role} \"{row.name}\"")

    return ActResult(ok=False, detail=f"unknown action {decision.action!r}")


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
    hwnd = observer.current_hwnd()
    if hwnd:
        user32.SetForegroundWindow(hwnd)
    for vk in modifiers:
        _send_key(vk)
    for key in keys:
        vk = VK_CODES.get(key) or (ord(key.upper()[:1]) if key else 0)
        _send_key(vk)
        _send_key(vk, keyup=True)
    for vk in reversed(modifiers):
        _send_key(vk, keyup=True)


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
    item = _INPUT(type=INPUT_KEYBOARD)
    item.ki = _KEYBDINPUT(wScan=ord(char), dwFlags=KEYEVENTF_UNICODE)
    _send_input(item)
    item.ki = _KEYBDINPUT(wScan=ord(char), dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
    _send_input(item)
    time.sleep(0.005)  # without a beat, Windows drops rapid unicode events

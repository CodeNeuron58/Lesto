"""Stage 4, Act: perform the chosen action.

Prefer accessibility actions (Invoke, SetValue) on Windows and DevTools input in the
browser; real mouse and keyboard input is the fallback. Anything irreversible
(send, pay, delete, submit) asks the user first.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .decide import Decision
from .observe.browser import BrowserError, BrowserObserver
from .observe.table import ActionRow

# Words that make an action irreversible; the user must approve before it runs.
RISKY_WORDS = ("send", "pay", "submit", "delete", "post", "approve", "transfer", "confirm",
               "place order", "checkout")
# Keyboard shortcuts that trigger the same words (e.g. Ctrl+Enter sends in Outlook).
RISKY_KEYS = ("ctrl+enter", "ctrl+s", "ctrl+d", "shift+delete")


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


async def act(observer: BrowserObserver, table: ActionTable, decision: Decision) -> ActResult:
    """Perform the decision's action on the page; never raises, reports via ActResult."""
    started = time.perf_counter()
    try:
        result = await _act(observer, table, decision)
    except BrowserError as exc:
        result = ActResult(ok=False, detail=f"devtools error: {exc}")
    result.ms = int((time.perf_counter() - started) * 1000)
    return result


async def _act(observer: BrowserObserver, table: ActionTable, decision: Decision) -> ActResult:
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

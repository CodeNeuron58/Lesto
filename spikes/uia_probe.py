"""UI Automation probe: how well does a Windows app describe its controls, and how fast can we read them?

Read-only: it never clicks or types. Run it, then click into the app you want to test:

    uv run python spikes/uia_probe.py            # 3-second countdown, 5 timed runs
    uv run python spikes/uia_probe.py --delay 5 --runs 10

Results are printed and saved to runs/ (gitignored, because they contain what's on screen).
"""

import argparse
import ctypes
import json
import os
import statistics
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import comtypes
import comtypes.client

# Load Windows' UI Automation library. comtypes reads its type information and generates
# Python bindings, so UIA.<name> gives us its interfaces and constants (property ids, etc.).
UIA = comtypes.client.GetModule("UIAutomationCore.dll")

# The UI Automation "client": our side of every conversation with an app.
uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

# Properties we want for every control (facts about it).
PROPERTIES = [
    UIA.UIA_NamePropertyId,  # its label, e.g. "Save"
    UIA.UIA_LocalizedControlTypePropertyId,  # its kind in words, e.g. "button"
    UIA.UIA_AutomationIdPropertyId,  # a fixed id the developer gave it (best for stable ids)
    UIA.UIA_IsEnabledPropertyId,  # can it be used right now?
    UIA.UIA_BoundingRectanglePropertyId,  # where it is on screen (never used as an id)
]

# Patterns say what you can DO with a control. We ask "is this pattern available?" for each.
PATTERNS = {
    "invoke": UIA.UIA_IsInvokePatternAvailablePropertyId,  # click it without the mouse
    "value": UIA.UIA_IsValuePatternAvailablePropertyId,  # set its text directly
    "toggle": UIA.UIA_IsTogglePatternAvailablePropertyId,  # checkboxes, toggle buttons
    "expand": UIA.UIA_IsExpandCollapsePatternAvailablePropertyId,  # dropdowns, tree nodes, menus
    "select": UIA.UIA_IsSelectionItemPatternAvailablePropertyId,  # list items, tabs, radio buttons
}


def countdown(seconds):
    """Give the user time to click into the app. We read the window in FRONT, which is the terminal until then."""
    print("Click into the app you want to probe...", end=" ", flush=True)
    for s in range(seconds, 0, -1):
        print(s, end=" ", flush=True)
        time.sleep(1)
    print()


def process_name(pid):
    """Turn a process id into its program name, e.g. EXCEL.EXE, using plain Windows calls."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
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


def foreground_window():
    """Find the window in front and turn it into a UI Automation element: the root we read from."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()  # Windows' handle for the window ("HWND")
    window = uia.ElementFromHandle(hwnd)
    return window, {
        "title": window.CurrentName,
        "process": process_name(window.CurrentProcessId),
        "class": window.CurrentClassName,
    }


def actionable_condition():
    """A filter UI Automation applies for us: on-screen, enabled controls that support at least one action.

    Tested on a Chrome-based app (Oct 31): this kept 150 of ~4,000 elements and was the fastest
    useful filter. Filtering by control type instead, or keeping off-screen controls, was slower.
    """
    any_pattern = None
    for pattern_property in PATTERNS.values():
        has_it = uia.CreatePropertyCondition(pattern_property, True)
        any_pattern = has_it if any_pattern is None else uia.CreateOrCondition(any_pattern, has_it)
    onscreen = uia.CreatePropertyCondition(UIA.UIA_IsOffscreenPropertyId, False)
    enabled = uia.CreatePropertyCondition(UIA.UIA_IsEnabledPropertyId, True)
    return uia.CreateAndCondition(onscreen, uia.CreateAndCondition(enabled, any_pattern))


def read_slow(window):
    """The naive way: find EVERY element, then ask for each one's name and type separately.

    Every .Current... read is a round trip into the app's process, so this costs
    (number of elements x properties) round trips.
    """
    start = time.perf_counter()
    elements = window.FindAll(UIA.TreeScope_Descendants, uia.CreateTrueCondition())
    find_ms = (time.perf_counter() - start) * 1000

    start = time.perf_counter()
    failed = 0
    for i in range(elements.Length):
        try:
            element = elements.GetElement(i)
            element.CurrentName
            element.CurrentLocalizedControlType
        except comtypes.COMError:  # elements can vanish while we read them
            failed += 1
    read_ms = (time.perf_counter() - start) * 1000
    return {"elements": elements.Length, "find_ms": find_ms, "read_ms": read_ms,
            "total_ms": find_ms + read_ms, "failed_reads": failed}


def read_fast(window, condition):
    """The Quiff way: one "cache request" brings back only actionable controls, with all their properties.

    We hand Windows a shopping list (which controls, which properties) and it returns
    everything in one batch. Afterwards, .Cached... reads are local, with no more round trips.
    """
    cache = uia.CreateCacheRequest()
    for property_id in PROPERTIES + list(PATTERNS.values()):
        cache.AddProperty(property_id)
    # "None" = don't keep live links to the app's controls, only the cached facts. Lighter and faster.
    cache.AutomationElementMode = UIA.AutomationElementMode_None

    start = time.perf_counter()
    elements = window.FindAllBuildCache(UIA.TreeScope_Descendants, condition, cache)
    ms = (time.perf_counter() - start) * 1000

    rows = []
    for i in range(elements.Length):
        element = elements.GetElement(i)
        rect = element.CachedBoundingRectangle
        rows.append({
            "type": element.CachedLocalizedControlType or "?",
            "name": (element.CachedName or "").strip(),
            "automation_id": element.CachedAutomationId or "",
            "patterns": [p for p, pid in PATTERNS.items() if element.GetCachedPropertyValue(pid)],
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
        })
    return rows, ms


def label_quality(rows):
    """How many actionable controls can a decision model actually understand? Thresholds are rough rules of thumb."""
    total = len(rows)
    named = sum(1 for r in rows if r["name"])
    with_id = sum(1 for r in rows if r["automation_id"])
    named_pct = 100 * named / total if total else 0
    if total < 5:
        verdict = "BAD: almost nothing exposed (the app may draw its own screen)"
    elif named_pct >= 80:
        verdict = "GOOD"
    elif named_pct >= 40:
        verdict = "PARTIAL"
    else:
        verdict = "BAD: most controls have no name"
    return {"actionable": total, "named": named, "named_pct": round(named_pct, 1),
            "with_automation_id": with_id, "with_id_pct": round(100 * with_id / total, 1) if total else 0,
            "verdict": verdict}


def format_rows(rows):
    """Render controls the way Quiff's decision model would see them: one numbered row each."""
    lines = []
    for n, r in enumerate(rows, 1):
        name = r["name"][:50] + ("…" if len(r["name"]) > 50 else "")
        line = f'[{n}] {r["type"][:14]:<14} "{name}"'
        if r["automation_id"]:
            line += f'  id={r["automation_id"][:30]}'
        lines.append(line)
    return lines


def save_results(result):
    """Write everything to runs/, which git ignores because it can contain private screen text."""
    folder = Path(__file__).resolve().parent.parent / "runs"
    folder.mkdir(exist_ok=True)
    app = Path(result["window"]["process"]).stem.lower() or "unknown"
    path = folder / f"probe-{datetime.now():%Y-%m-%d-%H%M%S}-{app}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay", type=int, default=3, help="seconds to switch to the target app")
    parser.add_argument("--runs", type=int, default=5, help="how many times to time the fast read")
    args = parser.parse_args()

    countdown(args.delay)
    window, info = foreground_window()
    print(f'Window: "{info["title"]}"   program: {info["process"]}   class: {info["class"]}')

    # Warm-up: Chrome-based apps (Chrome, VS Code, Electron apps) only build their accessibility
    # tree after something asks. The first read can be nearly empty, so we ask once, wait a moment
    # and throw the result away. (A probe may sleep; Quiff itself will wait on events instead.)
    condition = actionable_condition()
    read_fast(window, condition)
    time.sleep(1)

    print("Reading the slow way (may take a few seconds)...")
    slow = read_slow(window)

    timings, rows = [], []
    for _ in range(args.runs):
        rows, ms = read_fast(window, condition)
        timings.append(ms)

    quality = label_quality(rows)
    table = format_rows(rows)
    table_chars = len("\n".join(table))

    print()
    print(f'{"Slow way (everything):":<42}{slow["elements"]:>6,} elements   '
          f'{slow["total_ms"]:>7.0f} ms  (find {slow["find_ms"]:.0f} + read {slow["read_ms"]:.0f})')
    print(f'{"Fast way (batched, on-screen actionable):":<42}{quality["actionable"]:>6,} controls   '
          f'{statistics.median(timings):>7.0f} ms median of {args.runs}  (min {min(timings):.0f}, max {max(timings):.0f})')
    print(f'Labels: {quality["named"]}/{quality["actionable"]} named ({quality["named_pct"]}%)   '
          f'{quality["with_automation_id"]} with AutomationId ({quality["with_id_pct"]}%)')
    print(f"Action table size: {table_chars:,} characters, roughly {table_chars // 4:,} tokens")
    print(f'Verdict: {quality["verdict"]}')
    print()
    print("Sample rows (first 20):")
    for line in table[:20]:
        print("  " + line)

    path = save_results({
        "window": info,
        "slow": slow,
        "fast": {"runs_ms": timings, "median_ms": statistics.median(timings)},
        "quality": quality,
        "table_chars": table_chars,
        "rows": rows,
    })
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()

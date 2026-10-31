"""The automatic success checks for tasks: plain code, never model judgement.

Implements the ten check types from tasks/README.md. Browser checks await the
running page; file checks read the task's sandbox directory.
"""

from __future__ import annotations

import csv
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class PageView(Protocol):
    """What browser checks need from the running page."""

    async def url(self) -> str: ...

    async def title(self) -> str: ...

    async def body_text(self) -> str: ...

    async def dom_property(self, selector: str, property: str) -> Any: ...


@dataclass
class CheckResult:
    check_type: str
    ok: bool
    detail: str


async def run_checks(
    checks: list[dict[str, Any]],
    *,
    answer: str | None,
    page: PageView | None,
    sandbox: Path,
    started_ts: float,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        kind = check.get("type", "")
        try:
            ok, detail = await _run_one(
                check, kind, answer=answer, page=page, sandbox=sandbox, started_ts=started_ts
            )
        except Exception as exc:  # a broken check must neither pass silently nor crash the run
            ok, detail = False, f"check raised: {exc!r}"
        results.append(CheckResult(check_type=kind, ok=bool(ok), detail=detail))
    return results


async def _run_one(check: dict, kind: str, *, answer, page, sandbox, started_ts) -> tuple[bool, str]:
    if kind == "url":
        url = await page.url() if page else ""
        matched = re.search(check["pattern"], url) is not None
        return matched, f"url {url!r} {'matches' if matched else 'does not match'} {check['pattern']!r}"

    if kind == "page_text":
        text = await page.body_text() if page else ""
        wanted = check["contains"]
        ok = wanted in text
        return ok, f"page text {'contains' if ok else 'lacks'} {wanted!r}"

    if kind == "dom":
        prop = check.get("property", "value")
        value = await page.dom_property(check["selector"], prop) if page else None
        expected = check["equals"]
        return (value == expected), f"{check['selector']}.{prop} is {value!r} (want {expected!r})"

    if kind == "answer":
        if answer is None:
            return False, "no answer given"
        matched = re.search(check["pattern"], answer) is not None
        return matched, f"answer {answer!r} {'matches' if matched else 'does not match'} {check['pattern']!r}"

    if kind == "file":
        return _check_file(check, sandbox, started_ts)

    if kind == "missing":
        gone = not (sandbox / check["path"]).exists()
        return gone, f"{check['path']} {'is gone' if gone else 'still exists'}"

    if kind == "dir":
        ok = (sandbox / check["path"]).is_dir()
        return ok, f"{check['path']} {'is a directory' if ok else 'is not a directory'}"

    if kind == "csv":
        return _check_csv(check, sandbox)

    if kind == "zip":
        return _check_zip(check, sandbox)

    if kind == "window_title":
        title = await page.title() if page else ""
        matched = re.search(check["pattern"], title) is not None
        return matched, f"window title {title!r} vs {check['pattern']!r}"

    return False, f"unknown check type {kind!r}"


def _check_file(check: dict, sandbox: Path, started_ts: float) -> tuple[bool, str]:
    glob = check.get("glob")
    if glob:
        expanded = Path(glob).expanduser()
        matches = sorted(expanded.parent.glob(expanded.name))
        where = str(expanded.parent)
    else:
        target = sandbox / check["path"]
        matches = [target] if target.exists() else []
        where = str(sandbox)
    if not matches:
        return False, f"no file matches in {where}"
    if check.get("created_after_start") and matches[0].stat().st_mtime < started_ts - 1.0:
        return False, f"{matches[0].name} existed before the run started"
    content = matches[0].read_text(encoding="utf-8", errors="replace")
    if "equals" in check:
        ok = content.strip() == str(check["equals"]).strip()
        return ok, f"{matches[0].name} {'equals' if ok else 'differs from'} the wanted text"
    if "contains" in check:
        ok = check["contains"] in content
        return ok, f"{matches[0].name} {'contains' if ok else 'lacks'} {check['contains']!r}"
    return True, f"{matches[0].name} exists"


def _check_csv(check: dict, sandbox: Path) -> tuple[bool, str]:
    path = sandbox / check["path"]
    if not path.exists():
        return False, f"{check['path']} does not exist"
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [[str(cell) for cell in row] for row in csv.reader(handle) if row]
    if "rows" in check:
        wanted = [[str(cell) for cell in row] for row in check["rows"]]
        ok = rows == wanted
        return ok, "csv rows match" if ok else f"csv rows differ: got {rows[:3]}…"
    if "last_row" in check:
        wanted = [str(cell) for cell in check["last_row"]]
        last = rows[-1] if rows else []
        ok = last == wanted
        return ok, f"last row matches {wanted}" if ok else f"last row is {last}, want {wanted}"
    return False, "csv check needs rows or last_row"


def _check_zip(check: dict, sandbox: Path) -> tuple[bool, str]:
    path = sandbox / check["path"]
    if not path.exists():
        return False, f"{check['path']} does not exist"
    wanted = list(check.get("contains_files", []))
    with zipfile.ZipFile(path) as archive:
        names = [name.rsplit("/", 1)[-1] for name in archive.namelist()]
    missing = [name for name in wanted if name not in names]
    ok = not missing
    return ok, "zip contains every wanted file" if ok else f"zip lacks {missing}"

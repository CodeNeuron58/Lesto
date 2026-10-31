"""The action table shared by browser and desktop.

One numbered row per control a user can act on, e.g. `[12] button "Save" · enabled`.
Each row gets a stable id from its role, name and parent path, never from screen coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NAME_LIMIT = 60
VALUE_LIMIT = 40


@dataclass(frozen=True)
class ActionRow:
    """One control a user can act on."""

    index: int
    """The number shown in the table, 1-based. The decision model answers with this."""

    role: str
    """Accessible role: button, link, textbox, ..."""

    name: str
    """Accessible name, empty when the control has none."""

    actions: tuple[str, ...]
    """What Quiff may do to it: click, type, select."""

    enabled: bool
    value: str | None = None
    """Current text for editable controls, else None."""

    ref: str = ""
    """Stable id built from role + name + parent path (roles joined by <)."""

    target: Any = None
    """Opaque handle for the acting stage. Browser: backendDOMNodeId. Desktop: an IUIA element."""


@dataclass(frozen=True)
class ActionTable:
    """Every control on one screen that Quiff can act on."""

    title: str
    rows: tuple[ActionRow, ...]
    url: str | None = None
    read_ms: int | None = None

    def by_index(self, index: int) -> ActionRow | None:
        for row in self.rows:
            if row.index == index:
                return row
        return None

    def render(self) -> str:
        lines = [f"# {self.title}" + (f" — {self.url}" if self.url else "")]
        for row in self.rows:
            line = f'[{row.index}] {row.role} "{_clean(row.name, NAME_LIMIT)}"'
            if row.value is not None:
                line += f' · value="{_clean(row.value, VALUE_LIMIT)}"'
            if not row.enabled:
                line += " · disabled"
            lines.append(line)
        return "\n".join(lines)

    def approx_tokens(self) -> int:
        """Rough cost of sending the rendered table to a model (~4 chars per token)."""
        return sum(len(line) for line in self.render().splitlines()) // 4 + 2 * len(self.rows)


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace('"', "'")

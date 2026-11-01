"""Per-app skill files: written app knowledge injected into decisions.

A skill file is a short markdown recipe for one app — how it exposes its UI,
the flows every use of the app shares, and the quirks that cost us debugging
sessions. The runner loads the file for the task's app and injects it into
the decision context, so the model gets "how this app works" knowledge in
front of it instead of re-deriving it by exploration.

Cua ships the same idea as markdown Skills packs; the content here is our
own, grown from live runs. Keep each file tight: it rides along on every
decision of every task for that app.
"""

from __future__ import annotations

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


def load(app: str) -> str | None:
    """The skill text for one app, or None when no skill file exists."""
    path = SKILLS_DIR / f"{app}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def available() -> list[str]:
    """Apps that have skill files."""
    if not SKILLS_DIR.exists():
        return []
    return sorted(p.stem for p in SKILLS_DIR.glob("*.md"))

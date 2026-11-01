"""Tests for the per-app skill files and their injection into runs."""

from pathlib import Path

import pytest

from quiff import skills
from quiff.decide import Decision
from quiff.tasks import get_task, load_tasks


def test_skill_files_exist_for_all_three_apps():
    assert skills.available() == ["chrome", "explorer", "notepad"]


def test_load_returns_content_for_known_apps():
    for app in ("chrome", "explorer", "notepad"):
        text = skills.load(app)
        assert text and len(text) > 100


def test_load_returns_none_for_unknown_apps():
    assert skills.load("photoshop") is None


@pytest.mark.asyncio
async def test_skill_text_reaches_the_decider(tmp_path, monkeypatch):
    # the loop passes the subgoal to the decider: a capturing decider must
    # see the app guidance in it
    captured = {}

    class CapturingDecider:
        async def decide(self, goal, subgoal, table, history,
                         last_result=None, progress=None):
            captured["subgoal"] = subgoal
            from quiff.decide import Decision
            return Decision(action="done", subgoal_done=True, confidence=1.0)

    import quiff.tasks as tasks_mod
    from quiff.observe.table import ActionRow, ActionTable

    task = get_task("d01", load_tasks())
    monkeypatch.setattr(tasks_mod, "sandbox_dir", lambda t: tmp_path / t.id)

    class FakeDesktopObserver:
        async def open(self, argv, cwd=None):
            return ActionTable(title="Untitled - Notepad", rows=(
                ActionRow(1, "document", "Text editor", ("type",), True, target=1),
            ))

        async def read(self):
            return self._table

        @property
        def _table(self):
            return ActionTable(title="Untitled - Notepad", rows=(
                ActionRow(1, "document", "Text editor", ("type",), True, target=1),
            ))

        async def wait_settled(self):
            return None

        def current_hwnd(self):
            return 0

    observer = FakeDesktopObserver()
    run = await tasks_mod.run_task(task, CapturingDecider(), observer,
                                   run_root=tmp_path)
    assert "App guidance for notepad" in captured["subgoal"]
    assert "Text editor" in captured["subgoal"]

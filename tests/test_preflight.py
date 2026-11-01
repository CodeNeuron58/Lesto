"""Tests for the desktop pre-flight: user documents are never killed."""

import pytest

import quiff.tasks as tasks_mod
from quiff.observe import desktop as desktop_mod
from quiff.tasks import Task, _preflight_desktop_app


def make_task():
    return Task(id="d01-notepad-save", app="notepad", split="dev",
                goal="write", start={"app": "notepad"}, checks=[])


@pytest.fixture
def fake_desktop(monkeypatch):
    """Fake the window helpers the pre-flight uses."""
    state = {"closed": [], "answered": []}

    state["windows"] = []
    state["titles"] = {}
    state["pids"] = {}
    monkeypatch.setattr(desktop_mod, "_top_windows_of_pid",
                        lambda pid: [w for w in state["windows"]
                                     if state["pids"].get(w) == pid])
    monkeypatch.setattr(desktop_mod, "_window_title",
                        lambda hwnd: state["titles"].get(hwnd, ""))
    monkeypatch.setattr(desktop_mod, "_visible_windows",
                        lambda: list(state["windows"]))
    monkeypatch.setattr(desktop_mod, "_pid_of", lambda hwnd: state["pids"].get(hwnd, 0))
    monkeypatch.setattr(desktop_mod, "WM_CLOSE", 0)

    class FakeUser32:
        def PostMessageW(self, hwnd, msg, w, l):
            state["closed"].append(hwnd)
            state["windows"] = [w for w in state["windows"] if w != hwnd]
            state["titles"].pop(hwnd, None)
            return 1
        def IsWindow(self, hwnd):
            return hwnd in state["windows"]

    monkeypatch.setattr(desktop_mod, "user32", FakeUser32())
    return state


def test_preflight_proceeds_when_notepad_not_running(monkeypatch, fake_desktop):
    monkeypatch.setattr(tasks_mod, "_notepad_pids", lambda: set())
    _preflight_desktop_app(make_task())  # must not raise


def test_preflight_closes_blank_and_refuses_on_user_documents(monkeypatch, fake_desktop):
    fake_desktop["windows"] = [101, 102]
    fake_desktop["titles"] = {101: "Untitled - Notepad", 102: "dates.txt - Notepad"}
    fake_desktop["pids"] = {101: 55, 102: 55}
    monkeypatch.setattr(tasks_mod, "_notepad_pids", lambda: {55})
    # window 101 is a blank leftover; 102 holds a user document
    with pytest.raises(RuntimeError, match="close them first"):
        _preflight_desktop_app(make_task())


def test_preflight_closes_leftover_blank_window(monkeypatch, fake_desktop):
    class RealUser32:
        def PostMessageW(self, hwnd, msg, w, l):
            fake_desktop["windows"].remove(hwnd)
            return 1
        def IsWindow(self, hwnd):
            return hwnd in fake_desktop["windows"]

    monkeypatch.setattr(desktop_mod, "user32", RealUser32())
    fake_desktop["windows"] = [201]
    monkeypatch.setattr(tasks_mod, "_notepad_pids", lambda: {55})
    _preflight_desktop_app(make_task())  # closes 201, no prompt, no refusal

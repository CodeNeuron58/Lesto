"""Tests for task loading, sandbox setup, and the scripted deciders."""

from quiff.decide import MatchDecider, MatchStep
from quiff.observe.table import ActionRow, ActionTable
from quiff.tasks import get_task, load_tasks, sandbox_dir, setup_sandbox


def test_suite_loads_20_tasks_with_checks():
    tasks = load_tasks()
    assert len(tasks) == 20
    assert all(t.checks for t in tasks)
    assert {t.split for t in tasks} == {"dev", "hidden"}


def test_every_task_id_is_unique():
    ids = [t.id for t in load_tasks()]
    assert len(ids) == len(set(ids))


def test_sandbox_setup_creates_files_and_dirs(tmp_path, monkeypatch):
    import quiff.tasks as tasks_mod
    task = get_task("d03", load_tasks())
    monkeypatch.setattr(tasks_mod, "sandbox_dir", lambda t: tmp_path / t.id)
    sandbox = tasks_mod.setup_sandbox(task)
    assert (sandbox / "draft.txt").read_text(encoding="utf-8") == "Quarterly report draft"


async def test_match_decider_finds_rows_and_reports_missing():
    from quiff.decide import MatchDecider
    from quiff.observe.table import ActionRow, ActionTable

    rows = (ActionRow(1, "textbox", "Username", ("type",), True),
            ActionRow(2, "button", "Login", ("click",), True))
    table = ActionTable(title="t", rows=rows)
    decider = MatchDecider([MatchStep("type", "textbox", "user", ("tom",)),
                            MatchStep("click", "button", "login")])
    d1 = await decider.decide("g", "s", table, [])
    assert d1.action == "type" and d1.row == 1
    d2 = await decider.decide("g", "s", table, [])
    assert d2.action == "click" and d2.row == 2


async def test_match_decider_honest_failure_on_missing_row():
    from quiff.decide import MatchDecider, MatchStep
    from quiff.observe.table import ActionTable

    table = ActionTable(title="t", rows=(ActionRow(1, "link", "Home", ("click",), True),))
    decider = MatchDecider([MatchStep("click", "button", "submit")])
    d = await decider.decide("g", "s", table, [])
    assert d.action == "done" and d.subgoal_done is False

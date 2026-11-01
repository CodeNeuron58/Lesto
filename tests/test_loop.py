"""Tests for the fast loop's regression-prone paths: waits, retry/rescue,
redaction, and the approval guard — all with fakes, no model, no browser."""

import json
from pathlib import Path

import pytest

from quiff.decide import Decision
from quiff.learn import StepLogger
from quiff.loop import FastLoop
from fakes import FakeBrowserObserver, FakeDecider, Rescuer, username_password_rows


def make_loop(tmp_path, decisions, fail_first=0, rescuer=None, approver=None):
    observer = FakeBrowserObserver(username_password_rows())
    logger = StepLogger(tmp_path / "steps.jsonl", "test")
    decider = FakeDecider(decisions, fail_first=fail_first)
    loop = FastLoop(observer, decider, logger, max_steps=25,
                    rescuer=rescuer, approver=approver)
    return loop, observer, logger


@pytest.mark.asyncio
async def test_wait_decision_reobserves_without_crash(tmp_path):
    # regression: the wait branch used to raise UnboundLocalError
    decisions = [Decision(action="wait"),
                 Decision(action="type", row=1, values=("tom",)),
                 Decision(action="done", subgoal_done=True)]
    loop, observer, _ = make_loop(tmp_path, decisions)
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert result.success  # the old UnboundLocalError used to kill the run here
    assert "done" in result.stop_reason or "script" in result.stop_reason


@pytest.mark.asyncio
async def test_decide_retry_succeeds_after_transient_error(tmp_path):
    decisions = [Decision(action="type", row=1, values=("tom",)),
                 Decision(action="done", subgoal_done=True)]
    loop, observer, _ = make_loop(tmp_path, decisions, fail_first=1)
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert result.success  # the transient error was absorbed


@pytest.mark.asyncio
async def test_decider_fails_twice_rescuer_takes_over(tmp_path):
    decisions = [Decision(action="done", subgoal_done=True)]
    rescuer = Rescuer(Decision(action="done", subgoal_done=True))
    loop, _, _ = make_loop(tmp_path, decisions, fail_first=99, rescuer=rescuer)
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert result.success
    assert rescuer.called == 1


@pytest.mark.asyncio
async def test_decider_and_rescuer_both_failing_stops_honestly(tmp_path):
    rescuer = Rescuer(Decision(action="done", subgoal_done=True))
    loop, _, _ = make_loop(tmp_path, [Decision(action="type", row=1)],
                           fail_first=99, rescuer=rescuer)
    # the rescuer decision references a done whose row is None -> fine;
    # make the rescuer raise instead to test the honest stop:
    class ExplodingRescuer:
        async def decide(self, *a, **k):
            raise RuntimeError("rescuer down")
    loop.rescuer = ExplodingRescuer()
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert "decider and rescuer failed" in result.stop_reason


@pytest.mark.asyncio
async def test_password_redacted_in_logs(tmp_path):
    decisions = [Decision(action="type", row=2, values=("hunter2",)),
                 Decision(action="done", subgoal_done=True)]
    loop, _, logger = make_loop(tmp_path, decisions)
    await loop.run("goal", start_url="http://x/", subgoal="goal")
    content = Path(logger.path).read_text(encoding="utf-8")
    assert "hunter2" not in content
    assert "[REDACTED]" in content


@pytest.mark.asyncio
async def test_username_not_redacted(tmp_path):
    decisions = [Decision(action="type", row=1, values=("tom",)),
                 Decision(action="done", subgoal_done=True)]
    loop, _, logger = make_loop(tmp_path, decisions)
    await loop.run("goal", start_url="http://x/", subgoal="goal")
    content = Path(logger.path).read_text(encoding="utf-8")
    assert '"tom"' in content


@pytest.mark.asyncio
async def test_approval_guard_blocks_without_approver(tmp_path):
    decisions = [Decision(action="click", row=4)]  # "Submit order" -> guard fires
    rows = username_password_rows() + (
        __import__("quiff").observe.table.ActionRow(
            index=4, role="button", name="Submit order", actions=("click",),
            enabled=True, target=4),
    )
    observer = FakeBrowserObserver(rows)
    logger = StepLogger(tmp_path / "s.jsonl", "test")
    decider = FakeDecider(decisions)
    loop = FastLoop(observer, decider, logger)
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert "needs approval" in result.stop_reason


@pytest.mark.asyncio
async def test_approval_guard_passes_with_approver(tmp_path):
    rows = username_password_rows() + (
        __import__("quiff").observe.table.ActionRow(
            index=4, role="button", name="Submit order", actions=("click",),
            enabled=True, target=4),
    )
    observer = FakeBrowserObserver(rows)
    logger = StepLogger(tmp_path / "s.jsonl", "test")
    decisions = [Decision(action="click", row=4),
                 Decision(action="done", subgoal_done=True)]
    decider = FakeDecider(decisions)

    async def approver(reason):
        return True

    loop = FastLoop(observer, decider, logger, approver=approver)
    result = await loop.run("goal", start_url="http://x/", subgoal="goal")
    assert result.success

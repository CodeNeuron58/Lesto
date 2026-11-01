"""Tests for the automatic task checks (plain code, no model)."""

from pathlib import Path

import pytest

from quiff.checks import run_checks


@pytest.fixture
def sandbox(tmp_path):
    d = tmp_path / "sandbox"
    d.mkdir()
    return d


async def test_url_pass_and_fail(sandbox):
    class Page:
        async def url(self): return "https://x.test/secure"
        async def title(self): return "t"
        async def body_text(self): return ""
        async def dom_property(self, s, p): return None

    ok, = await run_checks([{"type": "url", "pattern": "/secure$"}],
                           answer=None, page=Page(), sandbox=sandbox, started_ts=0)
    bad, = await run_checks([{"type": "url", "pattern": "/other$"}],
                            answer=None, page=Page(), sandbox=sandbox, started_ts=0)
    assert ok.ok and not bad.ok


async def test_answer_check(sandbox):
    ok, = await run_checks([{"type": "answer", "pattern": r"\b1991\b"}],
                           answer="Python appeared in 1991", page=None,
                           sandbox=sandbox, started_ts=0)
    assert ok.ok


async def test_file_missing_dir_csv_zip(sandbox, started=None):
    import time
    started = time.time()
    (sandbox / "final.txt").write_text("Quarterly report draft", encoding="utf-8")
    (sandbox / "archive").mkdir()
    (sandbox / "data.csv").write_text("Name,Score\nRavi,91\n", encoding="utf-8")
    import zipfile
    with zipfile.ZipFile(sandbox / "photos.zip", "w") as z:
        z.writestr("photos/beach.txt", "x")

    checks = [
        {"type": "file", "path": "final.txt", "equals": "Quarterly report draft"},
        {"type": "missing", "path": "draft.txt"},
        {"type": "dir", "path": "archive"},
        {"type": "csv", "path": "data.csv", "last_row": ["Ravi", "91"]},
        {"type": "zip", "path": "photos.zip", "contains_files": ["beach.txt"]},
    ]
    results = await run_checks(checks, answer=None, page=None, sandbox=sandbox,
                               started_ts=started)
    assert all(r.ok for r in results)


async def test_file_created_after_start_fails_for_old_files(sandbox):
    import os, time
    old = time.time() - 100
    target = sandbox / "old.txt"
    target.write_text("hi", encoding="utf-8")
    os.utime(target, (old, old))
    results = await run_checks(
        [{"type": "file", "path": "old.txt", "created_after_start": True}],
        answer=None, page=None, sandbox=sandbox, started_ts=time.time())
    assert not results[0].ok


async def test_unknown_check_type_fails_cleanly(sandbox):
    results = await run_checks([{"type": "telepathy"}], answer=None, page=None,
                               sandbox=sandbox, started_ts=0)
    assert not results[0].ok and "unknown check type" in results[0].detail

"""Tests for the acting stage: modifier keys, page-key guard, input fallback."""

import pytest

from quiff.act import _act_browser
from quiff.decide import Decision
from quiff.observe.table import ActionRow, ActionTable
from fakes import FakeBrowserObserver, FakeCDP


def make_observer(page_key='{"t":1}'):
    rows = (ActionRow(1, "textbox", "Username", ("type",), True, target=1),
            ActionRow(2, "button", "Login", ("click",), True, target=2))
    return FakeBrowserObserver(rows, page_key=page_key)


def make_table():
    return ActionTable(title="t", url="http://x/", rows=(
        ActionRow(index=1, role="textbox", name="Username", actions=("type",),
                  enabled=True, target=1),
        ActionRow(index=2, role="button", name="Login", actions=("click",),
                  enabled=True, target=2),
    ), page_key='{"t":1}')


@pytest.mark.asyncio
async def test_press_modifiers_reach_cdp():
    observer, table = make_observer(), make_table()
    observer.table = table  # not used by press; keep the attribute anyway
    await _act_browser(observer, table, Decision(action="press", values=("ctrl+s",)))
    keys = [p for m, p in observer.cdp.calls if m == "Input.dispatchKeyEvent"]
    downs = [k for k in keys if k["type"] == "rawKeyDown"]
    assert [k["key"] for k in downs] == ["ctrl", "s"]
    assert all(k["modifiers"] == 2 for k in downs)  # Ctrl bitmask per CDP


@pytest.mark.asyncio
async def test_page_key_guard_refuses_stale_page():
    observer = make_observer(page_key='{"t":2}')  # live page differs
    table = make_table()  # observed with {"t":1}
    result = await _act_browser(observer, table,
                                Decision(action="click", row=2))
    assert not result.ok
    assert "page changed" in result.detail


@pytest.mark.asyncio
async def test_type_readback_fallback_to_js():
    class MissyCDP(FakeCDP):
        """insertText silently misses (the warm-tab quirk) but JS commit works."""

        async def call(self, method, timeout=15.0, **params):
            if method == "Input.insertText":
                self.calls.append((method, params))
                return {}  # the text never lands
            return await super().call(method, timeout=timeout, **params)

    observer = make_observer()
    observer.cdp = MissyCDP()
    table = make_table()
    result = await _act_browser(observer, table,
                                Decision(action="type", row=1, values=("tom",)))
    assert result.ok
    js_sets = [p for m, p in observer.cdp.calls
               if m == "Runtime.callFunctionOn" and "this.value = t" in p.get("functionDeclaration", "")]
    assert js_sets  # the fallback ran


@pytest.mark.asyncio
async def test_type_verified_no_fallback_when_landed():
    observer, table = make_observer(), make_table()
    observer.cdp.value = "tom"  # insertText lands and readback agrees
    result = await _act_browser(observer, table,
                                Decision(action="type", row=1, values=("tom",)))
    assert result.ok
    assert not [p for m, p in observer.cdp.calls
                if m == "Runtime.callFunctionOn" and "this.value = t" in p.get("functionDeclaration", "")]


def test_f_keys_resolve_on_both_paths():
    # regression: "f2" used to resolve to the letter F (vk 0x46 / 70)
    from quiff.act import VK_CODES, _key_spec
    for i in range(1, 13):
        assert VK_CODES[f"f{i}"] == 0x70 + i - 1
        assert _key_spec(f"f{i}")[1] == 0x70 + i - 1


def test_delete_key_in_both_maps():
    from quiff.act import VK_CODES, _key_spec
    assert VK_CODES["delete"] == 0x2E
    assert _key_spec("delete") == ("Delete", 46, "")

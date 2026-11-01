"""Tests for the session-reconnect wrapper on long warm tabs."""

import pytest

from quiff.observe.browser import BrowserError
from fakes import FakeBrowserObserver


@pytest.mark.asyncio
async def test_recovering_retries_once_on_session_drop():
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def call(self, method, timeout=15.0, **params):
            self.calls += 1
            if self.calls == 1:
                raise BrowserError("Not attached to an active page")
            return {"recovered": True}

    observer = FakeBrowserObserver(())
    observer.cdp = Flaky()
    from quiff.act import _Recovering
    recovering = _Recovering(observer)
    result = await recovering.call("Runtime.evaluate")
    assert result == {"recovered": True}
    assert observer.reconnects == 1
    assert observer.cdp.calls == 2


@pytest.mark.asyncio
async def test_recovering_raises_other_errors_immediately():
    class Grumpy:
        async def call(self, method, timeout=15.0, **params):
            raise BrowserError("some other error")

    observer = FakeBrowserObserver(())
    observer.cdp = Grumpy()
    from quiff.act import _Recovering
    recovering = _Recovering(observer)
    with pytest.raises(BrowserError):
        await recovering.call("Runtime.evaluate")
    assert observer.reconnects == 0

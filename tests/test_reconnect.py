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


@pytest.mark.asyncio
async def test_real_websockets_drop_normalizes_to_browser_error():
    # the bug class from the pass-6 review: real socket drops raised
    # websockets exceptions, which the reconnect machinery never saw
    import websockets.exceptions

    from quiff.observe.browser import CDPConnection, BrowserError

    conn = CDPConnection("ws://127.0.0.1:1/unused")

    class DeadWS:
        async def send(self, msg):
            raise websockets.exceptions.ConnectionClosedError(1006, None)

        async def close(self):
            pass

    conn._ws = DeadWS()
    conn._event_arrived = __import__("asyncio").Condition()
    with pytest.raises(BrowserError):
        await conn.call("Page.navigate", url="http://x/")

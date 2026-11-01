"""Fakes for testing the loop and acting stages without a real browser."""

from quiff.models.meta import ModelReply
from quiff.observe.table import ActionRow, ActionTable


class FakeCDP:
    """Records CDP calls and answers the ones the acting stage makes."""

    def __init__(self, value=""):
        self.value = value
        self.calls: list[tuple] = []

    async def call(self, method: str, timeout: float = 15.0, **params):
        self.calls.append((method, params))
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj1"}}
        if method == "Runtime.callFunctionOn":
            fn = params.get("functionDeclaration", "")
            if "return this.value" in fn:
                return {"result": {"value": self.value}}
            if "getBoundingClientRect" in fn:
                return {"result": {"value": {"x": 10, "y": 10, "w": 40, "h": 20}}}
            if "this.value = t" in fn:
                self.value = params["arguments"][0]["value"]
                return {"result": {"value": self.value}}
            return {"result": {"value": None}}
        if method == "Input.insertText":
            self.value = params.get("text", "")
        return {}


class FakeBrowserObserver:
    """Duck-typed browser observer: fixed table, recording CDP fake."""

    def __init__(self, rows, page_key='{"t":1,"h":"http://x/"}'):
        self.rows = rows
        self.page_key_value = page_key
        self.cdp = FakeCDP()
        self.reconnects = 0
        self.fail_first_calls = 0

    def table(self):
        return ActionTable(title="Test page", rows=tuple(self.rows),
                           url="http://x/", page_key=self.page_key_value)

    async def open(self, url):
        return self.table()

    async def read(self):
        return self.table()

    async def wait_settled(self):
        return None

    async def page_key(self):
        return self.page_key_value

    def connection(self):
        return self.cdp

    async def target_info(self):
        return {"url": "http://x/", "title": "Test page"}

    async def reconnect(self):
        self.reconnects += 1

    async def call(self, method: str, timeout: float = 15.0, **params):
        if self.fail_first_calls > 0:
            self.fail_first_calls -= 1
            from quiff.observe.browser import BrowserError
            raise BrowserError("Not attached to an active page")
        return await self.cdp.call(method, timeout=timeout, **params)


def username_password_rows():
    return (
        ActionRow(index=1, role="textbox", name="Username", actions=("type",),
                  enabled=True, target=1),
        ActionRow(index=2, role="textbox", name="Password", actions=("type",),
                  enabled=True, target=2),
        ActionRow(index=3, role="button", name="Login", actions=("click",),
                  enabled=True, target=3),
    )


class FakeDecider:
    """Scripted decisions; can raise on the first call to test the retry."""

    def __init__(self, decisions, fail_first=0):
        self.decisions = list(decisions)
        self.fail_first = fail_first
        self.calls = 0

    async def decide(self, goal, subgoal, table, history, last_result=None, progress=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("transient API error")
        if self.calls <= self.fail_first + len(self.decisions):
            return self.decisions[self.calls - self.fail_first - 1]
        return self.decisions[-1]


class AlwaysDone:
    async def decide(self, goal, subgoal, table, history, last_result=None):
        from quiff.decide import Decision
        return Decision(action="done", subgoal_done=True, confidence=1.0)


class Rescuer:
    def __init__(self, decision):
        self.decision = decision
        self.called = 0

    async def decide(self, goal, subgoal, table, history, last_result=None):
        self.called += 1
        return self.decision

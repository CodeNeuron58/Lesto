"""Tests for the shared action table and flow record/replay."""

from quiff.observe.table import ActionRow, ActionTable
from quiff.recall import Flow, FlowStep, load_flow, save_flow


def make_table():
    rows = (
        ActionRow(index=1, role="textbox", name="Username", actions=("type",), enabled=True),
        ActionRow(index=2, role="checkbox", name="Subscribe", actions=("click",),
                  enabled=True, state="checked"),
        ActionRow(index=3, role="button", name="Go", actions=("click",), enabled=True),
    )
    return ActionTable(title="Test", rows=rows, url="http://test/")


def test_render_shows_state_and_value():
    text = make_table().render()
    assert '[1] textbox "Username"' in text
    assert '[2] checkbox "Subscribe" · checked' in text


def test_token_estimate_grows_with_rows():
    small = make_table()
    big = ActionTable(title="T", rows=small.rows * 5)
    assert big.approx_tokens() > small.approx_tokens()


def test_by_index_roundtrip():
    table = make_table()
    assert table.by_index(2).name == "Subscribe"
    assert table.by_index(99) is None


def test_flow_save_and_load_roundtrip(tmp_path):
    flow = Flow(goal="log in", task_id="t1",
                steps=[FlowStep(action="type", role="textbox", name="Username",
                                values=("tomsmith",)),
                       FlowStep(action="press", role="", name="", values=("Enter",))])
    path = save_flow(flow, directory=tmp_path)
    loaded = load_flow("t1", directory=tmp_path)
    assert loaded.goal == "log in"
    assert loaded.steps[0].values == ("tomsmith",)
    assert path.exists()


def test_consent_wall_collapses_to_accept_buttons():
    from quiff.observe.browser import _collapse_consent_walls
    rows = (
        ActionRow(1, "dialog", "Cookie consent", (), True),
        ActionRow(2, "button", "Accept all cookies", ("click",), True),
        ActionRow(3, "button", "Reject", ("click",), True),
        ActionRow(4, "link", "Some page", ("click",), True),
    )
    collapsed = _collapse_consent_walls(list(rows))
    assert [r.name for r in collapsed] == ["Accept all cookies"]


def test_off_site_links_demote_to_end():
    from quiff.observe.browser import _demote_off_site
    rows = (
        ActionRow(1, "link", "External partner", ("click",), True,
                  ref="https://partner.example.com/page"),
        ActionRow(2, "link", "Internal docs", ("click",), True,
                  ref="https://docs.mysite.test/setup"),
    )
    ordered = _demote_off_site(list(rows), "https://www.mysite.test/home")
    assert [r.name for r in ordered] == ["Internal docs", "External partner"]

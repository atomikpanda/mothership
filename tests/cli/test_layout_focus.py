from mship.cli.layout import decide_focus_action, tab_name_for


def test_tab_name_is_deterministic_and_id_based():
    assert tab_name_for("wi-20260721-abc") == tab_name_for("wi-20260721-abc")
    assert "wi-20260721-abc" in tab_name_for("wi-20260721-abc")


def test_decision_create_when_absent():
    assert decide_focus_action("wi-1", [], is_done=False) == "create"


def test_decision_go_to_when_present():
    assert decide_focus_action("wi-1", ["other", "wi-1"], is_done=False) == "go-to"


def test_decision_close_when_done_and_present():
    assert decide_focus_action("wi-1", ["wi-1"], is_done=True) == "close"


def test_decision_noop_when_done_and_absent():
    assert decide_focus_action("wi-1", ["other"], is_done=True) == "noop"

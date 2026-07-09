from mship.core.view.thread_links import resolve_thread_work_item


class _Item:
    def __init__(self, id, spec_id=None, task_slugs=None, thread_ids=None):
        self.id = id
        self.spec_id = spec_id
        self.task_slugs = task_slugs or []
        self.thread_ids = thread_ids or []


def test_prefers_explicit_thread_link():
    items = [_Item("wi-a", thread_ids=["t1"]), _Item("wi-b", spec_id="s1")]
    assert resolve_thread_work_item("t1", "s1", None, items) == "wi-a"


def test_falls_back_to_spec_then_task():
    items = [_Item("wi-b", spec_id="s1"), _Item("wi-c", task_slugs=["k1"])]
    assert resolve_thread_work_item("t9", "s1", None, items) == "wi-b"
    assert resolve_thread_work_item("t9", None, "k1", items) == "wi-c"


def test_none_when_no_relation():
    assert resolve_thread_work_item("t9", None, None, [_Item("wi-b", spec_id="s1")]) is None

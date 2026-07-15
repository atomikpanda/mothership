from mship.core.view.thread_links import index_thread_work_items, resolve_thread_work_item


class _Item:
    def __init__(self, id, spec_id=None, task_slugs=None, thread_ids=None):
        self.id = id
        self.spec_id = spec_id
        self.task_slugs = task_slugs or []
        self.thread_ids = thread_ids or []


class _Thread:
    def __init__(self, id, spec_id=None, task_slug=None):
        self.id = id
        self.spec_id = spec_id
        self.task_slug = task_slug


def test_prefers_explicit_thread_link():
    items = [_Item("wi-a", thread_ids=["t1"]), _Item("wi-b", spec_id="s1")]
    assert resolve_thread_work_item("t1", "s1", None, items) == "wi-a"


def test_falls_back_to_spec_then_task():
    items = [_Item("wi-b", spec_id="s1"), _Item("wi-c", task_slugs=["k1"])]
    assert resolve_thread_work_item("t9", "s1", None, items) == "wi-b"
    assert resolve_thread_work_item("t9", None, "k1", items) == "wi-c"


def test_none_when_no_relation():
    assert resolve_thread_work_item("t9", None, None, [_Item("wi-b", spec_id="s1")]) is None


def test_index_batches_direct_and_indirect_resolution():
    items = [
        _Item("wi-a", thread_ids=["t1"]),
        _Item("wi-b", spec_id="s1"),
        _Item("wi-c", task_slugs=["k1"]),
    ]
    threads = [
        _Thread("t1", spec_id="s1"),   # direct membership wins over its spec_id
        _Thread("t2", spec_id="s1"),   # indirect via spec_id
        _Thread("t3", task_slug="k1"),  # indirect via task_slug
        _Thread("t4"),                  # unowned -> None
    ]
    assert index_thread_work_items(threads, items) == {
        "t1": "wi-a", "t2": "wi-b", "t3": "wi-c", "t4": None,
    }


def test_index_never_yields_two_items_for_one_thread():
    # Direct membership is exclusive and outranks the indirect fallback, so a thread that
    # is ALSO indirectly linkable still resolves to exactly one (its direct) item.
    items = [_Item("wi-a", thread_ids=["t1"]), _Item("wi-b", spec_id="s1", task_slugs=["k1"])]
    out = index_thread_work_items([_Thread("t1", spec_id="s1", task_slug="k1")], items)
    assert out == {"t1": "wi-a"}


def test_index_guards_corrupt_item_to_none():
    class _Bad:
        id = "wi-bad"
        spec_id = None
        task_slugs = []

        @property
        def thread_ids(self):
            raise RuntimeError("corrupt work item")

    # A corrupt WorkItem must never blow up the whole list — resolution degrades to None.
    assert index_thread_work_items([_Thread("t1")], [_Bad()]) == {"t1": None}


def test_corrupt_item_degrades_only_its_own_threads():
    """One corrupt item alongside healthy ones degrades only its own threads — healthy items still
    resolve (the per-item guard is surgical, not a whole-index blank)."""
    class _Bad:
        id = "wi-bad"
        spec_id = None
        task_slugs = []

        @property
        def thread_ids(self):
            raise RuntimeError("corrupt work item")

    good = _Item("wi-good", thread_ids=["t-good"])
    out = index_thread_work_items([_Thread("t-good"), _Thread("t-orphan")], [good, _Bad()])
    assert out == {"t-good": "wi-good", "t-orphan": None}

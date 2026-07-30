from pathlib import Path

from mship.core.assumptions import AssumptionRow, AssumptionStore, SEED_ROWS


def test_seed_writes_seven_rows_and_is_idempotent(tmp_path):
    store = AssumptionStore(tmp_path)
    rows = store.seed()
    assert len(rows) == 7
    assert store.path == tmp_path / "docs" / "product_assumptions.md"
    assert store.path.is_file()
    again = AssumptionStore(tmp_path).seed()   # second call: no overwrite, same rows
    assert [r.axis for r in again] == [r.axis for r in rows]


def test_round_trip_preserves_all_columns(tmp_path):
    store = AssumptionStore(tmp_path)
    store.save(list(SEED_ROWS))
    loaded = store.load()
    assert loaded == list(SEED_ROWS)


def test_axes_are_normalized_in_order(tmp_path):
    store = AssumptionStore(tmp_path); store.seed()
    assert store.axes()[0] == "repo topology"
    assert "dispatched model" in store.axes()


def test_encrypted_mode_writes_enc_and_round_trips(tmp_path):
    store = AssumptionStore(tmp_path, mode="encrypted")
    store.save(list(SEED_ROWS))
    assert store.path.name.endswith(".md.enc")
    assert (tmp_path / "docs" / "product_assumptions.md").exists() is False
    assert AssumptionStore(tmp_path, mode="encrypted").load() == list(SEED_ROWS)


def test_load_missing_returns_empty(tmp_path):
    assert AssumptionStore(tmp_path).load() == []


def test_soft_cap_warns_over_twenty(tmp_path):
    rows = [AssumptionRow(axis=f"a{i}", options="x/y", position="x", triggers="t") for i in range(21)]
    store = AssumptionStore(tmp_path)
    warn = store.save(rows)  # returns a warning string (or None under cap)
    assert warn and "20" in warn


def test_pipe_in_cell_round_trips_without_row_loss(tmp_path):
    """A literal `|` in a free-text cell must survive save/load, not silently
    drop the row (Greptile Wave-2 review). Reachable once `mship assumptions
    add/edit` lets a human type arbitrary position/options text."""
    from mship.core.assumptions import AssumptionRow, AssumptionStore
    rows = [AssumptionRow(axis="repo topology", options="single | mono | meta",
                          position="meta | shipped together", triggers="git/*")]
    store = AssumptionStore(tmp_path)
    store.save(rows)
    assert store.load() == rows


def test_newline_in_cell_rejected_on_save(tmp_path):
    """A newline in a cell would break the one-row-per-line table and drop the
    row on load — reject it at the boundary (final-review #3)."""
    import pytest
    from mship.core.assumptions import AssumptionRow, AssumptionStore
    store = AssumptionStore(tmp_path)
    with pytest.raises(ValueError, match="newline"):
        store.save([AssumptionRow(axis="x", options="a/b", position="line1\nline2", triggers="t")])


def test_malformed_store_raises_on_load(tmp_path):
    """A malformed canonical file must FAIL LOUD, not silently return a reduced
    row set (which would let an incomplete plan pass + shrink injection)
    (Greptile #450)."""
    import pytest
    from mship.core.assumptions import AssumptionStore
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product_assumptions.md").write_text(
        "| axis | options | position | triggers |\n| -- | -- | -- | -- |\n| a | b | c |\n"  # 3 cells
    )
    with pytest.raises(ValueError, match="malformed"):
        AssumptionStore(tmp_path).load()


def test_duplicate_axes_rejected_on_save(tmp_path):
    """Two rows normalizing to the same axis would resolve one verdict and leave
    the second assumption undispositioned — a gate bypass. Reject at the write
    boundary (Greptile #451)."""
    import pytest
    from mship.core.assumptions import AssumptionRow, AssumptionStore
    store = AssumptionStore(tmp_path)
    with pytest.raises(ValueError, match="duplicate assumption axis"):
        store.save([
            AssumptionRow(axis="repo topology", options="a/b", position="x", triggers="t"),
            AssumptionRow(axis="Repo  Topology", options="a/b", position="y", triggers="t"),
        ])


def test_duplicate_axes_rejected_on_load(tmp_path):
    """The read boundary also rejects a hand-edited store with duplicate axes, so
    a duplicate can't reach the verdict→flag path from a file we didn't write
    (Greptile #451)."""
    import pytest
    from mship.core.assumptions import AssumptionStore
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "product_assumptions.md").write_text(
        "| axis | options | position | triggers |\n"
        "| -- | -- | -- | -- |\n"
        "| repo topology | a/b | x | t |\n"
        "| Repo Topology | a/b | y | t |\n"
    )
    with pytest.raises(ValueError, match="duplicate assumption axis"):
        AssumptionStore(tmp_path).load()

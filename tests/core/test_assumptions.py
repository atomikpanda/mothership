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

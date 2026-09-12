from pathlib import Path

import pytest
import yaml

from mship.core.run_target.models import TargetSelectionError
from mship.core.run_target.preferences import TargetPreference, TargetPreferenceStore


def test_preference_store_round_trips_safe_aliases_under_repo_and_profile(tmp_path: Path):
    store = TargetPreferenceStore(tmp_path)
    preference = TargetPreference(host_name="studio", target_alias="qa-phone")

    store.put("mobile-app", "ios-latest", preference)

    assert store.get("mobile-app", "ios-latest") == preference
    raw = yaml.safe_load((tmp_path / "run-target-preferences.yaml").read_text())
    assert raw == {
        "version": 1,
        "preferences": {
            "mobile-app": {"ios-latest": {"host_name": "studio", "target_alias": "qa-phone"}}
        },
    }


def test_preference_store_is_owner_private_and_removal_only_affects_requested_profile(tmp_path: Path):
    store = TargetPreferenceStore(tmp_path)
    store.put("mobile-app", "ios-latest", TargetPreference(host_name="studio", target_alias=None))
    store.put("mobile-app", "android", TargetPreference(host_name="air", target_alias="pixel"))

    store.remove("mobile-app", "ios-latest")

    assert store.get("mobile-app", "ios-latest") is None
    assert store.get("mobile-app", "android") == TargetPreference(host_name="air", target_alias="pixel")
    assert (tmp_path / "run-target-preferences.yaml").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "run-target-preferences.yaml.lock").stat().st_mode & 0o777 == 0o600


def test_preference_rejects_empty_and_private_data_is_not_permissively_parsed(tmp_path: Path):
    with pytest.raises(ValueError):
        TargetPreference(host_name=None, target_alias=None)

    path = tmp_path / "run-target-preferences.yaml"
    path.write_text("version: 1\npreferences: []\n")
    path.chmod(0o600)
    with pytest.raises(TargetSelectionError) as error:
        TargetPreferenceStore(tmp_path).get("mobile-app", "ios-latest")
    assert error.value.code == "preferences_invalid"


def test_preference_store_rejects_nonprivate_file_without_echoing_contents(tmp_path: Path):
    path = tmp_path / "run-target-preferences.yaml"
    path.write_text("credential: do-not-echo\n")
    path.chmod(0o644)

    with pytest.raises(TargetSelectionError) as error:
        TargetPreferenceStore(tmp_path).get("mobile-app", "ios-latest")

    assert error.value.code == "preferences_invalid"
    assert "do-not-echo" not in str(error.value)

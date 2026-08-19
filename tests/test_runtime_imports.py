import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    (
        "mship.cli.relay",
        "mship.core.daemon.host_tunnel",
        "mship.core.daemon.relay_link",
        "mship.core.daemon.run",
        "mship.core.relay.enroll",
        "mship.core.relay.fleet_token",
        "mship.core.relay.host_directory",
    ),
)
def test_runtime_modules_import(module):
    importlib.import_module(module)

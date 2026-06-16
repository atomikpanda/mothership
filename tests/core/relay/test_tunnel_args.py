from pathlib import Path
from mship.core.relay.config import RelayConfig
from mship.core.relay.tunnel import subdomain_for, build_tunnel_argv

def test_subdomain_slugs_workspace():
    assert subdomain_for("Mship Workspace") == "mship-workspace"

def test_build_tunnel_argv():
    rc = RelayConfig(host="relay.example.com", ssh_port=2222, user="tunnel")
    argv = build_tunnel_argv(rc, subdomain="mship-workspace", local_port=47100, key_path=Path("/k/relay_ed25519"))
    assert argv[0] == "ssh"
    assert "-R" in argv and "mship-workspace:80:localhost:47100" in argv
    assert "-p" in argv and "2222" in argv
    assert "-i" in argv and "/k/relay_ed25519" in argv
    assert argv[-1] == "tunnel@relay.example.com"
    # resilience options present
    assert "-o" in argv and "ExitOnForwardFailure=yes" in argv and "ServerAliveInterval=30" in argv

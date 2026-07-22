from mship.core.relay.tls_ask import tls_ask_allowed

RELAY = "mship-relay.atomikpanda.com"

def test_allows_enroll_host():
    assert tls_ask_allowed(f"enroll.{RELAY}", RELAY) is True

def test_rejects_gh_broker_host_after_fold():
    assert tls_ask_allowed(f"gh.{RELAY}", RELAY) is False  # folded into serve; no separate cert

def test_allows_opaque_subdomain():
    # An opaque (base32-HMAC) device subdomain still passes the cert allowlist.
    from mship.core.relay.tunnel import device_subdomain
    sub = device_subdomain("ground-control", "abc123", b"\x00" * 32)
    assert tls_ask_allowed(f"{sub}.{RELAY}", RELAY) is True

def test_rejects_random_host():
    assert tls_ask_allowed(f"random-host.{RELAY}", RELAY) is False

def test_allows_serve_subdomains():
    assert tls_ask_allowed(f"mship-workspace-92bbb7.{RELAY}", RELAY) is True
    assert tls_ask_allowed(f"x-000000.{RELAY}", RELAY) is True

def test_rejects_bare_apex():
    assert tls_ask_allowed(RELAY, RELAY) is False

def test_rejects_foreign_domain():
    assert tls_ask_allowed("example.com", RELAY) is False
    assert tls_ask_allowed(f"mship-workspace-92bbb7.evil.com", RELAY) is False

def test_rejects_lookalike_and_extra_levels():
    assert tls_ask_allowed(f"enroll.{RELAY}.evil.com", RELAY) is False
    assert tls_ask_allowed(f"a.enroll.{RELAY}", RELAY) is False
    assert tls_ask_allowed(f"x.mship-workspace-92bbb7.{RELAY}", RELAY) is False

def test_rejects_non_serve_label():
    assert tls_ask_allowed(f"random.{RELAY}", RELAY) is False
    assert tls_ask_allowed(f"foo-92bbbz.{RELAY}", RELAY) is False

def test_rejects_blank_and_whitespace():
    assert tls_ask_allowed("", RELAY) is False
    assert tls_ask_allowed("   ", RELAY) is False
    assert tls_ask_allowed(f"enroll.{RELAY}", "") is False

def test_rejects_relay_as_suffix_without_label_boundary():
    # the critical vector: relay_domain as a bare suffix with NO dot boundary
    assert tls_ask_allowed(f"evil{RELAY}", RELAY) is False

def test_rejects_trailing_dot_fqdn():
    assert tls_ask_allowed(f"enroll.{RELAY}.", RELAY) is False
    assert tls_ask_allowed(f"x-000000.{RELAY}.", RELAY) is False

def test_uppercase_host_is_normalized_and_allowed():
    # hostnames are case-insensitive; the predicate lowercases, so these match
    assert tls_ask_allowed(f"ENROLL.{RELAY}", RELAY) is True
    assert tls_ask_allowed(f"X-ABCDEF.{RELAY.upper()}", RELAY) is True

def test_rejects_leading_hyphen_label():
    assert tls_ask_allowed(f"-abcdef.{RELAY}", RELAY) is False

def test_rejects_double_leading_hyphen_label():
    # base must start alphanumeric; a lone-hyphen base must not slip through
    assert tls_ask_allowed(f"--abcdef.{RELAY}", RELAY) is False


def test_egress_label_is_allowed():
    assert tls_ask_allowed("egress.relay.example.com", "relay.example.com") is True

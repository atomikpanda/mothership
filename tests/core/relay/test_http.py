"""Whole-call relay deadlines must include native resolver ownership."""

import os
import socket
import ssl
import time

import httpx
import pytest

from mship.core.relay.http import request


def test_deadline_reaps_a_stalled_resolver(monkeypatch, tmp_path):
    """A cancelled HTTP coroutine must not wait for or abandon native DNS work."""
    marker = tmp_path / "resolver.pid"
    stub = '''
import os
from pathlib import Path
import socket
import time
def stalled_lookup(*args, **kwargs):
    Path(os.environ["RESOLVER_TEST_PID"]).write_text(str(os.getpid()))
    time.sleep(2)
    raise socket.gaierror(socket.EAI_AGAIN, "controlled resolver delay")
socket.getaddrinfo = stalled_lookup
'''
    (tmp_path / "sitecustomize.py").write_text(stub)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("RESOLVER_TEST_PID", str(marker))
    monkeypatch.setenv("NO_PROXY", "*")
    original = socket.getaddrinfo
    namespace = {}
    try:
        exec(stub, namespace)
    finally:
        socket.getaddrinfo = original
    monkeypatch.setattr(socket, "getaddrinfo", namespace["stalled_lookup"])
    started = time.monotonic()

    with pytest.raises(httpx.TimeoutException):
        request("GET", "http://resolver-deadline.invalid/", timeout=0.4)

    elapsed = time.monotonic() - started
    assert marker.exists(), "test must reach the native resolver"
    assert elapsed < 0.8, f"0.4s deadline returned only after {elapsed:.3f}s"
    pid = int(marker.read_text())
    assert pid != os.getpid(), "uninterruptible DNS must have a killable owner"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_deadline_reaps_resolver_during_interpreter_startup(monkeypatch, tmp_path):
    """The child must already have an owner before executing any resolver code."""
    marker = tmp_path / "starting.pid"
    (tmp_path / "sitecustomize.py").write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(5)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "*")
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        request("GET", "http://localhost/", timeout=0.4)
    assert time.monotonic() - started < 0.8
    assert marker.exists(), "test must reach child interpreter startup"
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def _use_local_addresses(monkeypatch, tmp_path, *, fail=False):
    # Control only OS resolution in the child, preserving real TCP, TLS and HTTP.
    result = (
        'raise socket.gaierror(socket.EAI_AGAIN, "controlled DNS unavailable")'
        if fail else
        'return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))] '
        '+ [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) '
        'for ip in ("127.0.0.2", "127.0.0.1")]'
    )
    (tmp_path / "sitecustomize.py").write_text(
        'import socket\n'
        'def lookup(host, port, *args, **kwargs):\n'
        f'    {result}\n'
        'socket.getaddrinfo = lookup\n'
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "*")


def test_all_addresses_are_tried_and_http_contract_is_preserved(monkeypatch, tmp_path):
    from tests.core.relay.http_server import response_server

    _use_local_addresses(monkeypatch, tmp_path)
    with response_server(drip=False) as peer:
        url = peer.url.replace("127.0.0.1", "relay.invalid")
        response = request(
            "POST", url + "/register", timeout=2,
            headers={"Authorization": "Bearer fixture"}, json={"key": "value"},
        )
        assert response.status_code == 200
        assert response.headers["X-Deadline-Test"] == "preserved"
        assert response.json() == {"padding": "x" * 80}
        assert peer.requests == [("POST", "/register", "Bearer fixture", b'{"key":"value"}')]
        assert peer.hosts == [url.split("://", 1)[1]]


def test_dns_failure_remains_an_httpx_connect_error(monkeypatch, tmp_path):
    _use_local_addresses(monkeypatch, tmp_path, fail=True)
    with pytest.raises(httpx.ConnectError, match="controlled DNS unavailable"):
        request("GET", "http://relay.invalid/", timeout=2)


def test_https_preserves_sni_host_and_certificate_verification(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from tests.core.relay.http_server import response_server

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "relay.invalid")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("relay.invalid")]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    names = []
    context.set_servername_callback(lambda _sock, hostname, _context: names.append(hostname))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
    _use_local_addresses(monkeypatch, tmp_path)

    with response_server(drip=False, tls_context=context) as peer:
        url = peer.url.replace("127.0.0.1", "relay.invalid")
        assert request("GET", url, timeout=2).status_code == 200
        assert names == ["relay.invalid"]
        assert peer.hosts == [url.split("://", 1)[1]]
        with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
            request("GET", url.replace("relay.invalid", "wrong.invalid"), timeout=2)

import pytest
from mship.core.relay.egress.credential import (
    Attachment, AttachmentHostError, github_token_attachment, Credential,
)


def test_apply_sets_authorization_header_for_allowed_host():
    headers: dict = {}
    github_token_attachment().apply(headers, host="github.com", value="ghs_secret")
    assert headers["Authorization"] == "token ghs_secret"


def test_apply_refuses_host_outside_lock():
    with pytest.raises(AttachmentHostError):
        github_token_attachment().apply({}, host="evil.example.com", value="ghs_secret")


def test_render_formats_value():
    att = Attachment(header="Authorization", template="token {value}",
                     hosts=("github.com",))
    assert att.render("abc") == "token abc"


def test_credential_carries_value_ttl_and_attachment():
    cred = Credential(value="ghs_x", expires_at="2026-07-22T02:00:00Z",
                      attach=github_token_attachment())
    assert cred.value == "ghs_x"
    assert cred.attach.header == "Authorization"

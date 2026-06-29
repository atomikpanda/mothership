import pytest
from mship.cli.relay import enroll_base_url

def test_explicit_enroll_url_wins():
    assert enroll_base_url(enroll_url="http://h:47180", relay_host="r.example.com",
                           config_host="c.example.com") == "http://h:47180"

def test_relay_host_derives_https_enroll_subdomain():
    assert enroll_base_url(enroll_url=None, relay_host="r.example.com",
                           config_host=None) == "https://enroll.r.example.com"

def test_falls_back_to_config_host():
    assert enroll_base_url(enroll_url=None, relay_host=None,
                           config_host="c.example.com") == "https://enroll.c.example.com"

def test_errors_when_nothing_given():
    with pytest.raises(ValueError):
        enroll_base_url(enroll_url=None, relay_host=None, config_host=None)

def test_strips_trailing_slash_on_override():
    assert enroll_base_url(enroll_url="http://h:47180/", relay_host=None,
                           config_host=None) == "http://h:47180"

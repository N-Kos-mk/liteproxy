import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from liteproxy.security import AccessVerifier, HostGuard


def allowed(guard: HostGuard, url: str) -> bool:
    return asyncio.run(guard.allowed(url))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[::ffff:192.168.0.1]/",
        "http://localhost/",
    ],
)
def test_private_addresses_are_blocked(url):
    assert not allowed(HostGuard(), url)


def test_public_ip_literal_is_allowed():
    assert allowed(HostGuard(), "https://8.8.8.8/")


@pytest.mark.parametrize("url", ["ftp://8.8.8.8/", "file:///etc/passwd", "javascript:alert(1)", "http:///x"])
def test_non_http_urls_are_blocked(url):
    assert not allowed(HostGuard(), url)


def test_block_domains_include_subdomains():
    guard = HostGuard(allow_private=True, block_domains=["doubleclick.net"])
    assert not allowed(guard, "https://ad.doubleclick.net/x")
    assert not allowed(guard, "https://doubleclick.net/")
    assert allowed(guard, "https://notdoubleclick.net/")


def test_allow_private_for_tests():
    assert allowed(HostGuard(allow_private=True), "http://127.0.0.1:8000/")


# ---------------------------------------------------------------- Access JWT

TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_verifier(key, allowed_emails=()):
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    return AccessVerifier(TEAM, AUD, allowed_emails, jwks_client=jwks)


def make_token(key, **overrides):
    claims = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "email": "me@example.com",
        "exp": int(time.time()) + 60,
        **overrides,
    }
    return jwt.encode(claims, key, algorithm="RS256")


def test_access_accepts_valid_token(key):
    assert make_verifier(key).verify(make_token(key))["email"] == "me@example.com"


def test_access_rejects_wrong_audience(key):
    with pytest.raises(jwt.InvalidAudienceError):
        make_verifier(key).verify(make_token(key, aud=["other"]))


def test_access_rejects_expired_token(key):
    with pytest.raises(jwt.ExpiredSignatureError):
        make_verifier(key).verify(make_token(key, exp=int(time.time()) - 60))


def test_access_rejects_other_email(key):
    verifier = make_verifier(key, allowed_emails=["Me@Example.com"])
    assert verifier.verify(make_token(key))  # 大文字小文字は区別しない
    with pytest.raises(jwt.InvalidTokenError):
        verifier.verify(make_token(key, email="someone@example.com"))

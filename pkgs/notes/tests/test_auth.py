"""Identity from a reverse proxy, and the refusal to be exposed without one.

There are no accounts in this app. These two things are the whole of its
security posture that lives in code — everything else is the deployment's job
— so they get the same corpus treatment as path scoping.
"""

from __future__ import annotations

import pytest
from fastapi import Request

from notes.app import normalize_who, proxy_who, viewer_for, who
from notes.config import Settings
from notes.__main__ import check_exposure, is_loopback


def req(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw.append((b"cookie", jar.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


# ---------------------------------------------------------------- normalising

@pytest.mark.parametrize("raw,want", [
    ("ana", "ana"),
    ("Ana", "ana"),
    ("  ana  ", "ana"),
    ("ana@example.com", "ana"),                 # the common proxy shape
    ("ANA@CORP.EXAMPLE.COM", "ana"),
    ("ana.jones@example.com", "ana-jones"),     # dots are not in WHO_RE
    ("ana jones", "ana-jones"),
    ("m", "m"),
    ("m" * 40, "m" * 32),                         # truncated, still valid
    ("", None),
    (None, None),
    ("   ", None),
    ("@example.com", None),                       # no local part
    ("123", None),                                # must start with a letter
    ("-ana", "ana"),                            # leading separator stripped
    ("../../etc/passwd", "etc-passwd"),           # cannot become a path
    # A newline-bearing value cannot inject a second header or escape into a
    # path: every run of non-[a-z0-9_-] collapses to a single "-".
    ("ana\nRemote-User: admin", "ana-remote-user-admin"),
])
def test_normalize(raw, want):
    assert normalize_who(raw) == want


# ------------------------------------------------------------- trust is opt-in

def test_header_ignored_unless_trusted():
    cfg = Settings(space_dir="/nonexistent")
    assert cfg.trust_proxy_auth is False, "trusting a header must never be the default"
    r = req({"Remote-User": "admin"})
    assert proxy_who(r, cfg) is None
    assert who(r, cfg) is None


def test_header_used_when_trusted():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert who(req({"Remote-User": "ana"}), cfg) == "ana"


def test_header_name_is_configurable():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True,
                   auth_user_header="X-authentik-username")
    r = req({"X-authentik-username": "ana", "Remote-User": "admin"})
    assert who(r, cfg) == "ana", "only the configured header counts"


@pytest.mark.parametrize("header,value,want", [
    ("Remote-User", "ana", "ana"),                                   # Authelia
    ("X-Forwarded-User", "ana", "ana"),                              # oauth2-proxy
    ("X-authentik-username", "ana", "ana"),                          # Authentik
    ("Cf-Access-Authenticated-User-Email", "ana@ex.com", "ana"),     # Cloudflare Access
    ("Tailscale-User-Login", "ana@ex.com", "ana"),                   # tailscale serve
])
def test_the_proxies_people_actually_run(header, value, want):
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True, auth_user_header=header)
    assert who(req({header: value}), cfg) == want


def test_proxy_beats_the_cookie():
    """A cookie is the browser's claim about itself; the proxy has checked."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    r = req({"Remote-User": "ana"}, {"notes_who": "sam"})
    assert who(r, cfg) == "ana"


def test_cookie_still_works_without_a_proxy():
    cfg = Settings(space_dir="/nonexistent")
    assert who(req(cookies={"notes_who": "ana"}), cfg) == "ana"


def test_garbage_cookie_is_not_an_identity():
    cfg = Settings(space_dir="/nonexistent")
    assert who(req(cookies={"notes_who": "../../etc"}), cfg) == "etc"
    assert who(req(cookies={"notes_who": ""}), cfg) is None


def test_a_trusted_header_that_is_absent_falls_back():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert who(req(cookies={"notes_who": "sam"}), cfg) == "sam"


# ------------------------------------------------------------- exposure guard

@pytest.mark.parametrize("host,loop", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.53", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.10", False), ("notes.example.com", False),
])
def test_loopback_detection(host, loop):
    assert is_loopback(host) is loop


def test_loopback_needs_no_permission():
    assert check_exposure(Settings(space_dir="/nonexistent")) is None


def test_public_bind_is_refused_by_default():
    problem = check_exposure(Settings(space_dir="/nonexistent", host="0.0.0.0"))
    assert problem is not None
    assert "NO authentication" in problem


def test_public_bind_allowed_when_said_out_loud():
    cfg = Settings(space_dir="/nonexistent", host="0.0.0.0", allow_public_bind=True)
    assert check_exposure(cfg) is None


def test_public_bind_allowed_behind_proxy_auth():
    cfg = Settings(space_dir="/nonexistent", host="0.0.0.0", trust_proxy_auth=True)
    assert check_exposure(cfg) is None


def test_an_unresolvable_host_counts_as_public():
    """Fail closed on anything we cannot prove is loopback."""
    assert check_exposure(Settings(space_dir="/nonexistent", host="notes.example.com")) is not None


# --------------------------------------------- whose feed a listing is built for

def test_me_param_works_without_auth():
    """One browser previewing the other person's wall — the reason ?me= exists."""
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(cookies={"notes_who": "sam"}), cfg, "ana") == "ana"


def test_me_param_cannot_override_an_authenticated_user():
    """Otherwise proxy auth is bypassable with a query string."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    r = req({"Remote-User": "ana"}, {"notes_who": "ana"})
    assert viewer_for(r, cfg, "sam") == "ana"
    assert viewer_for(r, cfg, "admin") == "ana"
    assert viewer_for(r, cfg, None) == "ana"


def test_viewer_falls_back_to_the_default_identity():
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(), cfg, None) == "sam"


def test_a_junk_me_param_does_not_become_the_viewer():
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(cookies={"notes_who": "ana"}), cfg, "../../etc") == "ana"


# ------------------------------------------------- the default author is config

def test_the_default_author_comes_from_configuration():
    """⚠️ Not cosmetic: every note predating authorship is attributed to this."""
    from notes import store
    from notes.app import create_app
    before = store.HUMAN
    try:
        create_app(Settings(space_dir="/nonexistent", default_author="Sam"))
        assert store.HUMAN == "sam", "slugged, then applied"
        create_app(Settings(space_dir="/nonexistent", default_author="!!!"))
        assert store.HUMAN == "sam", "an unusable value must not blank it"
    finally:
        store.HUMAN = before

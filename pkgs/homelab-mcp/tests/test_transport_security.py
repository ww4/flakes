"""DNS-rebinding protection must know the public hostname.

Regression cover for the defect that made the funnel deploy fail before it
shipped: the MCP SDK auto-enables DNS-rebinding protection whenever the bind
address is loopback, allowing only 127.0.0.1/localhost Host headers. Behind a
reverse proxy the Host is the public name, so every request came back 421
Misdirected Request — with no MCP-level error, which makes it read like a
network fault rather than a configuration one.

These assert against the SDK's real matching logic (TransportSecurityMiddleware
._validate_host) rather than against our own list, so the tests still fail if a
future SDK changes how wildcards match.
"""

from __future__ import annotations

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware

from homelab_mcp.config import Settings
from homelab_mcp.server import build_transport_security

PUBLIC = "gromit.example-tailnet.ts.net"


def _accepts(settings: Settings, host: str) -> bool:
    middleware = TransportSecurityMiddleware(build_transport_security(settings))
    return middleware._validate_host(host)


@pytest.fixture()
def published(tmp_path) -> Settings:
    return Settings(space_root=tmp_path, public_host=PUBLIC)


def test_loopback_only_keeps_the_sdk_default(tmp_path):
    """No public host means we must not weaken anything — defer to the SDK."""
    assert build_transport_security(Settings(space_root=tmp_path)) is None


def test_public_host_is_accepted_without_a_port(published):
    """The real case: https:// on 443 sends a bare hostname, no port.

    The SDK's "host:*" wildcard only matches a Host that carries an explicit
    port, so listing only the wildcard form would still 421 in production.
    """
    assert _accepts(published, PUBLIC)


def test_public_host_is_accepted_with_a_port(published):
    assert _accepts(published, f"{PUBLIC}:443")


def test_loopback_still_accepted_when_published(published):
    """Post-deploy verification probes the service on loopback."""
    assert _accepts(published, "127.0.0.1:8787")
    assert _accepts(published, "localhost:8787")


@pytest.mark.parametrize(
    "host",
    [
        "evil.example.com",
        # A prefix of the real name, and a suffix of it — neither may pass.
        "gromit.example-tailnet.ts.net.evil.com",
        "not-gromit.example-tailnet.ts.net",
        f"{PUBLIC}evil.com",
        "",
    ],
)
def test_other_hosts_are_rejected(published, host):
    assert not _accepts(published, host)


def test_protection_is_actually_on(published):
    """A list of allowed hosts means nothing if the check is disabled."""
    settings = build_transport_security(published)
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True

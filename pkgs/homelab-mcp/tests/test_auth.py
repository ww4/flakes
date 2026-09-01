"""Header authentication — the gate that demotes the path prefix to defence in depth.

Exercised through the real ASGI interface rather than by calling the predicate
functions, because the thing that can silently break is the *middleware wiring*,
not the string comparison. An earlier version of this file shipped a
`check_bearer` that was never called from anywhere and looked, from the outside,
exactly like working auth.
"""

from __future__ import annotations

import asyncio

import pytest

from homelab_mcp.config import Settings
from homelab_mcp.server import TokenAuthMiddleware, check_api_key, check_bearer

TOKEN = "s3cr3t-token-value"


class _Sink:
    """A trivial ASGI app that records whether it was reached."""

    def __init__(self) -> None:
        self.reached = False

    async def __call__(self, scope, receive, send) -> None:
        self.reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _request_async(settings: Settings, headers: dict[str, str], scope_type: str = "http"):
    """Drive the middleware directly and return (status, headers, downstream_reached)."""
    sink = _Sink()
    mw = TokenAuthMiddleware(sink, settings)
    scope = {
        "type": scope_type,
        "path": "/prefix/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw(scope, receive, send)
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    hdrs = {
        k.decode().lower(): v.decode()
        for m in sent
        if m["type"] == "http.response.start"
        for k, v in m.get("headers", [])
    }
    return status, hdrs, sink.reached


@pytest.fixture()
def guarded(tmp_path) -> Settings:
    return Settings(space_root=tmp_path, mcp_token=TOKEN)


@pytest.fixture()
def open_server(tmp_path) -> Settings:
    return Settings(space_root=tmp_path)


# --- the middleware is actually in the request path ------------------------


def test_correct_bearer_reaches_the_app(guarded):
    status, _, reached = _request(guarded, {"Authorization": f"Bearer {TOKEN}"})
    assert reached and status == 200


def test_correct_api_key_reaches_the_app(guarded):
    """The connector UI picks the header name; both standard forms must work."""
    status, _, reached = _request(guarded, {"X-API-Key": TOKEN})
    assert reached and status == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},                                          # nothing at all
        {"Authorization": "Bearer wrong"},           # wrong secret
        {"Authorization": TOKEN},                    # right secret, missing scheme
        {"Authorization": "Basic " + TOKEN},         # wrong scheme
        {"X-API-Key": "wrong"},                      # wrong secret
        {"Authorization": f"Bearer {TOKEN} "},       # trailing space is not the token
        {"Authorization": f"bearer {TOKEN}"},        # lowercase scheme is not accepted
    ],
)
def test_unauthenticated_never_reaches_the_app(guarded, headers):
    status, hdrs, reached = _request(guarded, headers)
    assert not reached, "request reached the MCP app without a valid credential"
    assert status == 401
    assert "bearer" in hdrs.get("www-authenticate", "").lower()


def test_header_name_is_case_insensitive(guarded):
    """ASGI lowercases header names, but be explicit — HTTP does not care."""
    status, _, reached = _request(guarded, {"AUTHORIZATION": f"Bearer {TOKEN}"})
    assert reached and status == 200


def test_no_token_configured_means_no_check(open_server):
    """A local run without a token must not require one."""
    status, _, reached = _request(open_server, {})
    assert reached and status == 200


def test_non_http_scope_passes_through(guarded):
    """Lifespan events must not be answered with 401."""

    async def drive():
        sink = _Sink()
        mw = TokenAuthMiddleware(sink, guarded)

        async def send(message):
            pass

        async def receive():
            return {"type": "lifespan.startup"}

        await mw({"type": "lifespan"}, receive, send)
        return sink.reached

    assert asyncio.run(drive())


# --- the comparison itself -------------------------------------------------


def test_predicates_reject_the_empty_and_missing_cases(guarded):
    assert not check_bearer(guarded, None)
    assert not check_bearer(guarded, "")
    assert not check_api_key(guarded, None)
    assert not check_api_key(guarded, "")


def test_predicates_are_permissive_only_when_unconfigured(open_server):
    assert check_bearer(open_server, None)
    assert check_api_key(open_server, None)


def _request(settings, headers, scope_type="http"):
    """Sync wrapper — keeps the suite free of an async pytest plugin dependency."""
    return asyncio.run(_request_async(settings, headers, scope_type))

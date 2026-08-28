"""MCP tool surface — six tools, no more.

Each additional tool is additional blast radius, so the set is fixed:

  reads   get_context, search_notes, read_note
  writes  save_note, append_note, request_work

There is deliberately no delete tool, no arbitrary-path write, and no
"call any SilverBullet endpoint" passthrough.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import audit
from .config import Settings
from .context import build_context
from .paths import PathRejected
from .queue import append_request

# Aliased on import so the tool functions below cannot accidentally shadow —
# and then infinitely recurse into — the implementations they wrap.
from .space import append_note as space_append
from .space import read_note as space_read
from .space import save_note as space_save
from .space import search_notes as space_search


def build_transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """DNS-rebinding protection that knows the public hostname.

    The SDK turns this protection on by itself whenever the bind address is
    loopback, with `allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]`.
    That default is right for a server nothing proxies, and wrong the moment one
    sits behind a reverse proxy: the Host header then carries the public name,
    matches nothing, and every request is refused with 421 Misdirected Request.

    That failure is worth spelling out because of how it presents. It is a bare
    transport-layer refusal with no MCP-level error, so the connector reports a
    generic failure to connect and the obvious suspects are DNS, TLS, the
    firewall and the tunnel — none of which are at fault. Found by probing the
    proxy with a forged PROXY-protocol header before deploying, not after.

    Returning None keeps the SDK's own default, which is the correct behaviour
    for the loopback-only case.

    Note the bare `public_host` alongside `public_host:*`: the SDK's wildcard
    form only matches a Host that actually carries a port, and a browser or
    server hitting https:// on 443 sends the bare name.
    """
    if not settings.public_host:
        return None

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            settings.public_host,
            f"{settings.public_host}:*",
            # Kept so a loopback probe on the box still works — that is how the
            # service is checked after a deploy.
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ],
        allowed_origins=[
            f"https://{settings.public_host}",
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ],
    )


def build_server(settings: Settings) -> FastMCP:
    audit.configure()

    queue_path = settings.resolved_queue_page()  # fails loudly at boot if misconfigured

    mcp = FastMCP(
        name="homelab",
        instructions=(
            "Chris's homelab knowledgebase (a SilverBullet space on the 'gromit' NixOS box). "
            "Call get_context() first in any conversation about the homelab, his projects, "
            "or his tasks — it carries the conventions, active projects, open tasks and the "
            "deployed service inventory. Use save_note to capture something worth keeping, "
            "and request_work to hand a concrete job to the agent running on the box."
        ),
        # Stateless: no per-session map to grow without bound. The prior art
        # leaks session state indefinitely; not holding any is the fix.
        stateless_http=True,
        streamable_http_path=settings.mcp_path,
        host=settings.host,
        port=settings.port,
        transport_security=build_transport_security(settings),
    )

    # ------------------------------------------------------------------ reads

    @mcp.tool(name="get_context")
    def get_context() -> str:
        """Orientation for this homelab and knowledgebase.

        Returns the space's conventions, the landing page, active projects and
        areas, current open tasks, and the list of services deployed on the
        box. Call this before answering questions about Chris's homelab,
        projects or tasks — without it you are guessing.
        """
        return build_context(
            settings.space_root,
            flake_root=settings.flake_root,
            include_service_inventory=settings.include_service_inventory,
            context_page=settings.context_page,
        )

    @mcp.tool(name="search_notes")
    def search_notes_tool(query: str, limit: int = 20) -> list[dict[str, str]]:
        """Search the knowledgebase by literal text.

        All whitespace-separated tokens must appear, in the page body or its
        path; matching is case-insensitive. Regular expressions are not
        supported. Returns path, title and a short excerpt per hit.
        """
        hits = space_search(settings.space_root, query, limit)
        return [{"path": h.path, "title": h.title, "excerpt": h.excerpt} for h in hits]

    @mcp.tool(name="read_note")
    def read_note_tool(path: str) -> str:
        """Read one page in full, by the path returned from search_notes."""
        try:
            return space_read(settings.space_root, path)
        except PathRejected as exc:
            audit.record_rejection("read_note", str(exc))
            raise

    # ----------------------------------------------------------------- writes

    @mcp.tool(name="save_note")
    def save_note_tool(
        title: str,
        body: str,
        tags: list[str] | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """Save a NEW note to the inbox for Chris to triage later.

        Use this to capture an idea, a research result, or a conclusion worth
        keeping. Never overwrites: on a name collision a numeric suffix is
        added. Returns the final path so you can tell Chris where it landed.
        """
        try:
            saved = space_save(
                settings.space_root,
                settings.inbox_dir,
                title=title,
                body=body,
                tags=tags,
                source_url=source_url,
            )
        except PathRejected as exc:
            audit.record_rejection("save_note", str(exc))
            raise
        audit.record_write("save_note", saved.path, saved.bytes_written)
        return {"path": saved.path, "bytes_written": saved.bytes_written}

    @mcp.tool(name="append_note")
    def append_note_tool(path: str, body: str) -> dict[str, Any]:
        """Append to an existing inbox note, by the path save_note returned.

        Only notes inside the inbox can be appended to. Content is added
        verbatim — no substitution is performed on it.
        """
        try:
            saved = space_append(settings.space_root, settings.inbox_dir, path, body)
        except PathRejected as exc:
            audit.record_rejection("append_note", str(exc))
            raise
        audit.record_write("append_note", saved.path, saved.bytes_written)
        return {"path": saved.path, "bytes_written": saved.bytes_written}

    @mcp.tool(name="request_work")
    def request_work_tool(
        title: str,
        what: str,
        why: str | None = None,
        urgency: str = "whenever",
    ) -> dict[str, Any]:
        """Ask the agent running on the homelab box to do a concrete job.

        Files a task on the agent's work queue; it is picked up on the agent's
        next scheduled run, not immediately. Describe the work concretely in
        `what` — the agent acts on this without the chat's context. `urgency`
        is one of: whenever, soon, today.

        This is a request, not a command: Chris sees the queue and can drop or
        edit anything on it.
        """
        queued = append_request(
            queue_path,
            settings.space_root,
            title=title,
            what=what,
            why=why,
            urgency=urgency,
        )
        audit.record_write("request_work", queued.page, queued.bytes_written)
        return {
            "page": queued.page,
            "status": "queued",
            "note": "Picked up on the agent's next scheduled run, not immediately.",
        }

    return mcp


def check_bearer(settings: Settings, header_value: str | None) -> bool:
    """Constant-time bearer check. Dormant until a connector can send a header.

    Compared with `hmac.compare_digest`, not `!=` — the prior art uses a plain
    comparison, which leaks the token a character at a time under timing
    analysis. Never read from a query parameter: the MCP authorization spec
    prohibits access tokens in the query string.
    """
    if not settings.mcp_token:
        return True
    if not header_value or not header_value.startswith("Bearer "):
        return False
    presented = header_value[len("Bearer ") :]
    return hmac.compare_digest(presented, settings.mcp_token)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    server = build_server(settings)
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()

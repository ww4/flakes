"""Configuration. Everything via environment / `.env` — no hardcoded values."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HOMELAB_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- the space -------------------------------------------------------
    space_root: Path = Field(
        default=Path("/var/lib/silverbullet"),
        description="Root of the SilverBullet space.",
    )
    inbox_dir: str = Field(
        default="Inbox",
        description="Folder inside the space that captures land in. The ONLY writable folder.",
    )
    queue_page: str = Field(
        default="System/Agent Queue.md",
        description="Page that request_work appends to. Not caller-supplied; validated at startup.",
    )

    # --- transport -------------------------------------------------------
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8787)
    path_prefix: str = Field(
        default="",
        description=(
            "Unguessable path prefix, e.g. a 32-char hex string from `openssl rand -hex 16`. "
            "The endpoint becomes /<prefix>/mcp. Empty means /mcp (local testing only)."
        ),
    )
    public_host: str = Field(
        default="",
        description=(
            "The public hostname this server is reached at, e.g. gromit.<tailnet>.ts.net. "
            "Empty means loopback-only. This is NOT cosmetic: the MCP SDK auto-enables "
            "DNS-rebinding protection whenever the bind address is loopback, and its "
            "allowlist is then 127.0.0.1/localhost only — so a request arriving through a "
            "proxy with the real public Host header is rejected 421 Misdirected Request. "
            "Naming the public host here is what makes the server answerable through the "
            "funnel. See build_transport_security()."
        ),
    )

    # --- auth ------------------------------------------------------------
    mcp_token: str | None = Field(
        default=None,
        description=(
            "Optional bearer token. DORMANT: as of 2026-07-27 the Claude custom-connector "
            "UI offers only a URL plus OAuth client id/secret — there is no custom-header "
            "field, so nothing can send this. Kept wired so it can be switched on the day "
            "one appears, without a code change."
        ),
    )

    # --- context composition --------------------------------------------
    include_service_inventory: bool = Field(
        default=True,
        description="Include a generated host/service inventory derived from the flake.",
    )
    flake_root: Path = Field(
        default=Path("/home/claude/flakes"),
        description="Read-only source for the service inventory.",
    )
    context_page: str = Field(
        default="Areas/Agent Context.md",
        description=(
            "A curated page in the space, read in full into get_context(). "
            "This replaces exposing the agent's open-loops board. The board is "
            "a working task list, and its open sections are by nature a list of "
            "unremediated work — including unremediated SECURITY work, with "
            "hostnames attached. That is the wrong artifact for an endpoint "
            "whose only authenticator is a URL. This page is curated instead: "
            "nothing lands in it that Chris or the agent did not deliberately "
            "put there, and it fails safe."
        ),
    )

    @field_validator("path_prefix")
    @classmethod
    def _clean_prefix(cls, value: str) -> str:
        return value.strip("/")

    @field_validator("inbox_dir")
    @classmethod
    def _clean_inbox(cls, value: str) -> str:
        cleaned = value.strip("/")
        if not cleaned or "/" in cleaned:
            raise ValueError("inbox_dir must be a single folder name")
        return cleaned

    @property
    def mcp_path(self) -> str:
        return f"/{self.path_prefix}/mcp" if self.path_prefix else "/mcp"

    def resolved_queue_page(self) -> Path:
        """Absolute path of the queue page, asserted to live inside the space.

        Checked once at startup rather than per request: this value comes from
        the operator, never from a caller, so there is no traversal surface —
        but a typo that pointed it outside the space should fail loudly at boot
        rather than write somewhere surprising at runtime.
        """
        root = self.space_root.resolve()
        candidate = (self.space_root / self.queue_page).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("queue_page must resolve inside the space")
        if candidate.suffix != ".md":
            raise ValueError("queue_page must be a .md page")
        return candidate

"""Configuration. Everything via environment / .env — no hardcoded credentials."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # STACKS_-prefixed for our own knobs; DATABASE_URL is conventionally
    # unprefixed and is picked up in get_settings() below.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="STACKS_",
        extra="ignore",
    )

    database_url: str = ""

    # Open Library asks clients to identify themselves and to cache rather than
    # hammer. Both are courtesy requirements of a non-profit service, and we
    # honour them: see stacks.enrich.openlibrary.
    ol_user_agent: str = "stacks/0.1 (personal book catalog)"
    ol_max_concurrency: int = 4
    ol_min_interval_ms: int = 350
    ol_base: str = "https://openlibrary.org"
    ol_covers_base: str = "https://covers.openlibrary.org"

    hardcover_token: str = ""

    #: Where fetched cover images are kept. Open Library rate-limits cover
    #: lookups by identifier to 100 per IP per five minutes, so a browse wall
    #: is only viable if each cover is fetched once and then served locally.
    cover_cache_dir: str = "data/covers"


def get_settings() -> Settings:
    import os

    s = Settings()
    # DATABASE_URL is conventionally unprefixed; the devshell exports it.
    if not s.database_url:
        s.database_url = os.environ.get("DATABASE_URL", "")
    return s

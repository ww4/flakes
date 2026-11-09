"""Configuration — environment only, never baked into the store."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHOEBOX_", env_file=".env", extra="ignore")

    space_dir: str = "/var/lib/silverbullet"
    host: str = "127.0.0.1"
    port: int = 3337

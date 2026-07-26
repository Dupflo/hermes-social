from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    tiktok_backoffice_db: Path = Path("/opt/data/tiktok-backoffice/tiktok_backoffice.sqlite3")
    tiktok_ms_token_file: Path = Path("/opt/data/meta-comment-dm-automation/data/tiktok_login_tool_token.txt")
    tiktok_confirm_publish: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def publish_enabled(self) -> bool:
        # Deliberately false for the current draft-only implementation. The env
        # flag is reserved for a future, separately reviewed publisher.
        return False

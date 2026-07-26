from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_page_access_token: str = ""
    meta_page_id: str = ""
    meta_ig_user_id: str = ""
    meta_owner_ids: str = ""
    meta_owner_usernames: str = "dupflodev"

    resource_keyword: str = "proxy"
    resource_url: str = "https://example.com/resource"
    public_reply_text: str = "C'est envoyé, check tes DM"
    processed_comments_database: str = "data/processed_comments.sqlite3"
    interest_only_keywords: str = "migration"

    graph_api_version: str = Field(default="v21.0")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def get_settings() -> Settings:
    return Settings()

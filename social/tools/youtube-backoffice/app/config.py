from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    youtube_api_key: str = ""
    youtube_channel_id: str = ""
    youtube_owner_username: str = "dupflodev"
    resource_keyword: str = "proxy"
    resource_url: str = ""
    public_reply_text: str = "Voici le lien :"
    processed_comments_database: str = "/opt/data/youtube-backoffice/processed_comments.sqlite3"
    interest_only_keywords: str = "migration"
    graph_api_version: str = "v3"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_settings() -> Settings:
    return Settings()

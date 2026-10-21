from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    bot_token: str
    remnawave_url: str
    remnawave_token: str
    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    admin_ids: list[int] = []
    log_level: str = "INFO"
    mock_remnawave: bool = False


settings = Settings()

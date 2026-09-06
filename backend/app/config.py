from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: Literal["local", "test", "production"] = "local"
    APP_ORIGIN: str = "http://localhost:5173"
    RELEASE_SHA: str = "local-development"

    # Strict rejection of unknown application settings arrives with configuration tests.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

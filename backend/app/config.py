from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: Literal["local", "test", "production"] = "local"
    APP_ORIGIN: str = "http://localhost:5173"
    RELEASE_SHA: str = "local-development"
    DATABASE_URL: str = ""

    JWT_SECRET: str = ""
    JWT_ISSUER: str = "commonsbook"
    JWT_AUDIENCE: str = "commonsbook-web"
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    RATE_LIMIT_HMAC_SECRET: str = ""
    TEST_PROFILE: str = "standard"

    EMAIL_ADAPTER: Literal["console", "smtp"] = "console"
    EMAIL_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str = ""

    METRICS_TOKEN: str = ""
    SENTRY_DSN: str = ""
    GRAFANA_REMOTE_WRITE_URL: str = ""
    GRAFANA_REMOTE_WRITE_USER: str = ""
    GRAFANA_REMOTE_WRITE_TOKEN: str = ""
    API_INTERNAL_URL: str = ""

    # Strict rejection of unknown application settings arrives with configuration tests.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if self.APP_ENV == "production":
            if self.TEST_PROFILE == "race":
                raise ValueError("TEST_PROFILE 'race' is forbidden in production")
            if not self.JWT_SECRET or len(self.JWT_SECRET.encode("utf-8")) < 32:
                raise ValueError("JWT_SECRET must be at least 32 bytes in production")
            if (
                not self.RATE_LIMIT_HMAC_SECRET
                or len(self.RATE_LIMIT_HMAC_SECRET.encode("utf-8")) < 32
            ):
                raise ValueError("RATE_LIMIT_HMAC_SECRET must be at least 32 bytes in production")
            if not self.METRICS_TOKEN or len(self.METRICS_TOKEN.encode("utf-8")) < 32:
                raise ValueError("METRICS_TOKEN must be at least 32 bytes in production")
            if self.GRAFANA_REMOTE_WRITE_URL and not self.GRAFANA_REMOTE_WRITE_URL.startswith(
                "https://"
            ):
                raise ValueError("GRAFANA_REMOTE_WRITE_URL must be an HTTPS URL")
        else:
            if self.JWT_SECRET and len(self.JWT_SECRET.encode("utf-8")) < 32:
                raise ValueError("JWT_SECRET must be at least 32 bytes when set")
            if (
                self.RATE_LIMIT_HMAC_SECRET
                and len(self.RATE_LIMIT_HMAC_SECRET.encode("utf-8")) < 32
            ):
                raise ValueError("RATE_LIMIT_HMAC_SECRET must be at least 32 bytes when set")
            if self.METRICS_TOKEN and len(self.METRICS_TOKEN.encode("utf-8")) < 32:
                raise ValueError("METRICS_TOKEN must be at least 32 bytes when set")
            if self.GRAFANA_REMOTE_WRITE_URL and not self.GRAFANA_REMOTE_WRITE_URL.startswith(
                "https://"
            ):
                raise ValueError("GRAFANA_REMOTE_WRITE_URL must be an HTTPS URL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

import sentry_sdk
from sentry_sdk.types import Event, Hint

from app.config import get_settings


def init_sentry(service: str = "api") -> None:
    """Initialize Sentry SDK with scrubbing and sampling rules if SENTRY_DSN is configured."""
    settings = get_settings()
    if not settings.SENTRY_DSN or not settings.SENTRY_DSN.strip():
        return

    def before_send(event: Event, hint: Hint) -> Event | None:
        # Scrub request headers, body and query strings
        req = event.get("request")
        if isinstance(req, dict):
            headers = req.get("headers")
            if isinstance(headers, dict):
                for secret_header in (
                    "authorization",
                    "cookie",
                    "proxy-authorization",
                    "x-metrics-token",
                ):
                    if secret_header in headers:
                        headers[secret_header] = "[REDACTED]"
            if "data" in req:
                req["data"] = "[REDACTED]"
            if "query_string" in req:
                req["query_string"] = "[REDACTED]"

        return event

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN.strip(),
        release=settings.RELEASE_SHA,
        environment=settings.APP_ENV,
        send_default_pii=False,
        traces_sample_rate=0.1,
        before_send=before_send,
    )

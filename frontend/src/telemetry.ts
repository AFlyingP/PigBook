import * as Sentry from "@sentry/react";

/**
 * Initialize frontend telemetry with Sentry when VITE_SENTRY_DSN is configured.
 * When the DSN is empty or unset, zero network requests are made.
 */
export function initTelemetry(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN;
  if (!dsn || !dsn.trim()) {
    return;
  }

  Sentry.init({
    dsn: dsn.trim(),
    release: import.meta.env.VITE_RELEASE_SHA || undefined,
    sendDefaultPii: false,
    tracesSampleRate: 0.1,
    beforeSend(event) {
      if (event.request) {
        if (event.request.headers) {
          const sensitive = ["authorization", "cookie", "proxy-authorization"];
          for (const key of Object.keys(event.request.headers)) {
            if (sensitive.includes(key.toLowerCase())) {
              event.request.headers[key] = "[REDACTED]";
            }
          }
        }
        if (event.request.data) {
          event.request.data = "[Filtered]";
        }
        if (event.request.query_string) {
          event.request.query_string = "[Filtered]";
        }
      }
      return event;
    },
  });
}

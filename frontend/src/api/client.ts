import { clearAttempt } from "./createAttempt";
import type { components } from "./schema";

export type User = components["schemas"]["User"];

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(
    status: number,
    code: string,
    message: string,
    details: Record<string, unknown> = {}
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  skipAuth?: boolean;
  _retry?: boolean;
}

// In-memory token storage only - never persisted in localStorage or logs
let inMemoryAccessToken: string | null = null;
let lastTokenRefreshTime = 0;
let refreshPromise: Promise<void> | null = null;

type AuthChangeListener = (token: string | null, user?: User | null) => void;
const authListeners = new Set<AuthChangeListener>();

export function getAccessToken(): string | null {
  return inMemoryAccessToken;
}

export function setAccessToken(token: string | null): void {
  inMemoryAccessToken = token;
  if (token) {
    lastTokenRefreshTime = Date.now();
  }
}

export function subscribeToAuthChanges(listener: AuthChangeListener): () => void {
  authListeners.add(listener);
  return () => {
    authListeners.delete(listener);
  };
}

function notifyAuthListeners(token: string | null, user?: User | null): void {
  authListeners.forEach((fn) => {
    try {
      fn(token, user);
    } catch {
      // ignore
    }
  });
}

// Cross-tab broadcast channel
let authChannel: BroadcastChannel | null = null;
if (typeof BroadcastChannel !== "undefined") {
  try {
    authChannel = new BroadcastChannel("commonsbook-auth");
    authChannel.onmessage = (event) => {
      const msg = event.data;
      if (msg?.type === "TOKEN_REFRESHED") {
        inMemoryAccessToken = msg.token;
        lastTokenRefreshTime = Date.now();
        notifyAuthListeners(msg.token, msg.user);
      } else if (msg?.type === "LOGOUT") {
        inMemoryAccessToken = null;
        clearAttempt();
        notifyAuthListeners(null, null);
      }
    };
  } catch {
    // BroadcastChannel unsupported or blocked
  }
}

export function postAuthBroadcast(message: unknown): void {
  if (authChannel) {
    try {
      authChannel.postMessage(message);
    } catch {
      // ignore
    }
  }
}

/**
 * Perform token refresh with single-flight tab deduplication and
 * cross-tab Web Locks API synchronization (Spec 3.4, 7.2).
 */
export async function refreshSession(): Promise<void> {
  if (refreshPromise) {
    return refreshPromise;
  }

  const doRefresh = async () => {
    const res = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      credentials: "same-origin",
    });

    if (!res.ok) {
      let code = "INVALID_REFRESH";
      let message = "Session expired or invalid";
      let details = {};
      try {
        const body = await res.json();
        if (body?.error) {
          code = body.error.code || code;
          message = body.error.message || message;
          details = body.error.details || {};
        }
      } catch {
        // non-json response
      }
      setAccessToken(null);
      clearAttempt();
      notifyAuthListeners(null, null);
      postAuthBroadcast({ type: "LOGOUT" });
      throw new ApiError(res.status, code, message, details);
    }

    const data = (await res.json()) as {
      access_token: string;
      token_type: string;
      expires_in: number;
      user: User;
    };

    setAccessToken(data.access_token);
    lastTokenRefreshTime = Date.now();
    notifyAuthListeners(data.access_token, data.user);
    postAuthBroadcast({
      type: "TOKEN_REFRESHED",
      token: data.access_token,
      user: data.user,
    });
  };

  refreshPromise = (async () => {
    try {
      if (
        typeof navigator !== "undefined" &&
        navigator.locks &&
        typeof navigator.locks.request === "function"
      ) {
        await navigator.locks.request("commonsbook-refresh", async () => {
          // If another tab just completed the refresh while we waited for the lock, reuse it
          if (
            lastTokenRefreshTime &&
            Date.now() - lastTokenRefreshTime < 5000 &&
            inMemoryAccessToken
          ) {
            return;
          }
          await doRefresh();
        });
      } else {
        await doRefresh();
      }
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

/**
 * Spec 3.4 required request<T> signature.
 * Returns { data, etag? } or throws ApiError.
 * Automatically retries once on 401 via refreshSession.
 */
export async function request<T>(
  path: string,
  options?: RequestOptions
): Promise<{ data: T; etag?: string }> {
  const url = path.startsWith("/") ? path : `/${path}`;
  const headers: Record<string, string> = {
    ...(options?.headers as Record<string, string> | undefined),
  };

  if (!options?.skipAuth && inMemoryAccessToken) {
    headers["Authorization"] = `Bearer ${inMemoryAccessToken}`;
  }

  let bodyData: BodyInit | undefined;
  if (options?.body !== undefined) {
    if (
      typeof options.body === "string" ||
      (typeof FormData !== "undefined" && options.body instanceof FormData) ||
      (typeof Blob !== "undefined" && options.body instanceof Blob)
    ) {
      bodyData = options.body as BodyInit;
    } else {
      if (!headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
      }
      bodyData = JSON.stringify(options.body);
    }
  }

  const response = await fetch(url, {
    ...options,
    method: options?.method || (options?.body ? "POST" : "GET"),
    headers,
    body: bodyData,
    credentials: options?.credentials || "same-origin",
  });

  // Handle 401 with one-shot refresh retry
  if (
    response.status === 401 &&
    !options?._retry &&
    !url.includes("/auth/login") &&
    !url.includes("/auth/register") &&
    !url.includes("/auth/refresh")
  ) {
    try {
      await refreshSession();
      return await request<T>(path, {
        ...options,
        _retry: true,
      });
    } catch {
      // Refresh failed, fall through to error handling
    }
  }

  if (!response.ok) {
    let code = "UNKNOWN_ERROR";
    let message = response.statusText || "Request failed";
    let details: Record<string, unknown> = {};

    try {
      const errBody = await response.json();
      if (errBody?.error) {
        code = errBody.error.code || code;
        message = errBody.error.message || message;
        details = errBody.error.details || {};
      }
    } catch {
      // Non-JSON error body
    }

    throw new ApiError(response.status, code, message, details);
  }

  const rawEtag = response.headers.get("ETag");
  const etag = rawEtag ? rawEtag.replace(/^W\//, "").replace(/"/g, "") : undefined;

  if (response.status === 204) {
    return { data: undefined as unknown as T, etag };
  }

  const data = (await response.json()) as T;
  return { data, etag };
}

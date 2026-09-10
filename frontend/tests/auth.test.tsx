import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import {
  beginAttempt,
  restoreAttempt,
  clearAttempt,
  type CreateAttempt,
} from "../src/api/createAttempt";
import {
  request,
  setAccessToken,
  getAccessToken,
  refreshSession,
  ApiError,
} from "../src/api/client";
import { AuthProvider } from "../src/features/auth/AuthContext";
import { LoginForm } from "../src/features/auth/LoginForm";
import { RegisterForm } from "../src/features/auth/RegisterForm";

describe("CreateAttempt Recovery & Isolation (Spec 3.4, 7.3)", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  afterEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  it("begins an attempt and saves it to sessionStorage with UUID key and principal ID", () => {
    const principalId = "user-123-abc";
    const payload = { resource_id: "res-1", starts_at: "2026-09-15T14:00:00Z" };

    const attempt = beginAttempt(principalId, "booking", payload);

    expect(attempt.principalId).toBe(principalId);
    expect(attempt.kind).toBe("booking");
    expect(attempt.payload).toEqual(payload);
    expect(attempt.key).toMatch(/^[0-9a-f-]{36}$/i);
    expect(attempt.createdAt).toBeDefined();

    const stored = JSON.parse(sessionStorage.getItem("commonsbook_create_attempt") || "{}");
    expect(stored.key).toBe(attempt.key);
    expect(stored.principalId).toBe(principalId);
  });

  it("restores attempt only for matching authenticated principal", () => {
    const userA = "principal-user-a";
    const userB = "principal-user-b";
    const attempt = beginAttempt(userA, "booking", { slot: 1 });

    // Restoring with matching principal succeeds
    const restoredA = restoreAttempt(userA);
    expect(restoredA).not.toBeNull();
    expect(restoredA?.key).toBe(attempt.key);

    // Restoring with different principal clears and returns null (Spec 7.3)
    const restoredB = restoreAttempt(userB);
    expect(restoredB).toBeNull();
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("clears attempt and expires attempts older than 24 hours", () => {
    const user = "principal-user-c";
    const oldDate = new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString();
    const staleAttempt: CreateAttempt = {
      key: "stale-key-1",
      payload: {},
      kind: "booking",
      principalId: user,
      createdAt: oldDate,
    };
    sessionStorage.setItem("commonsbook_create_attempt", JSON.stringify(staleAttempt));

    const restored = restoreAttempt(user);
    expect(restored).toBeNull();
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();

    // clearAttempt works directly
    beginAttempt(user, "blackout", {});
    expect(sessionStorage.getItem("commonsbook_create_attempt")).not.toBeNull();
    clearAttempt();
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("ensures token responses never enter persistent frontend caches (localStorage/sessionStorage)", () => {
    setAccessToken("test-bearer-token-12345");
    expect(localStorage.getItem("token")).toBeNull();
    expect(localStorage.getItem("access_token")).toBeNull();
    expect(sessionStorage.getItem("token")).toBeNull();
    expect(sessionStorage.getItem("access_token")).toBeNull();
    setAccessToken(null);
  });
});

describe("API Client & Cross-Tab In-Memory Auth (Spec 3.4, 7.2)", () => {
  beforeEach(() => {
    setAccessToken(null);
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    setAccessToken(null);
  });

  it("stores access token in memory only and includes Bearer header in request", async () => {
    setAccessToken("mock-jwt-token");
    expect(getAccessToken()).toBe("mock-jwt-token");

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers({ ETag: '"v1"' }),
      json: async () => ({ id: "u-1", email: "test@example.com" }),
    } as Response);

    const res = await request<{ id: string }>("/api/v1/me");
    expect(res.data.id).toBe("u-1");
    expect(res.etag).toBe("v1");

    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/me",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer mock-jwt-token",
        }),
      })
    );
  });

  it("automatically retries once on 401 via refreshSession", async () => {
    setAccessToken("expired-token");

    // First call returns 401
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      json: async () => ({ error: { code: "INVALID_TOKEN", message: "Token expired" } }),
    } as Response);

    // Refresh call returns 200 with new token
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({
        access_token: "refreshed-jwt-token",
        token_type: "bearer",
        expires_in: 900,
        user: { id: "u-1", email: "test@example.com" },
      }),
    } as Response);

    // Retried call returns 200
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ success: true }),
    } as Response);

    const res = await request<{ success: boolean }>("/api/v1/data");
    expect(res.data.success).toBe(true);
    expect(getAccessToken()).toBe("refreshed-jwt-token");
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it("deduplicates simultaneous refreshSession calls within single flight", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      if (String(url).includes("/auth/refresh")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => ({
            access_token: "flight-token",
            token_type: "bearer",
            expires_in: 900,
            user: { id: "u-1" },
          }),
        } as Response;
      }
      return { ok: true, status: 200, headers: new Headers(), json: async () => ({}) } as Response;
    });

    await Promise.all([refreshSession(), refreshSession()]);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("handles expired session refresh failure by clearing token and raising ApiError", async () => {
    setAccessToken("some-token");

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      json: async () => ({
        error: { code: "INVALID_REFRESH", message: "Refresh token revoked" },
      }),
    } as Response);

    await expect(refreshSession()).rejects.toThrow(ApiError);
    expect(getAccessToken()).toBeNull();
  });
});

describe("LoginForm & RegisterForm Component Behavior (Spec 7.1, 7.2)", () => {
  beforeEach(() => {
    setAccessToken(null);
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderWithProviders(ui: React.ReactElement) {
    const qc = new QueryClient();
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AuthProvider>{ui}</AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("renders accessible LoginForm with visible focus, labels, and password reveal", async () => {
    renderWithProviders(<LoginForm />);

    const emailInput = screen.getByLabelText(/email address/i);
    const passwordInput = screen.getByLabelText(/^password/i);
    const submitBtn = screen.getByRole("button", { name: /sign in/i });
    const revealBtn = screen.getByRole("button", { name: /show password/i });

    expect(emailInput).toBeInTheDocument();
    expect(passwordInput).toBeInTheDocument();
    expect(submitBtn).toBeInTheDocument();
    expect(revealBtn).toBeInTheDocument();

    // Toggle password reveal
    expect(passwordInput).toHaveAttribute("type", "password");
    fireEvent.click(revealBtn);
    expect(passwordInput).toHaveAttribute("type", "text");
    fireEvent.click(revealBtn);
    expect(passwordInput).toHaveAttribute("type", "password");
  });

  it("supports keyboard-only form operation and displays error on disabled user / 401", async () => {
    const user = userEvent.setup();

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      json: async () => ({
        error: { code: "INVALID_CREDENTIALS", message: "Account disabled or invalid credentials" },
      }),
    } as Response);

    renderWithProviders(<LoginForm />);

    const emailInput = screen.getByLabelText(/email address/i);
    const passwordInput = screen.getByLabelText(/^password/i);

    await user.click(emailInput);
    await user.keyboard("member@example.com");
    await user.tab();
    expect(passwordInput).toHaveFocus();
    await user.keyboard("MyPassword123!");
    await user.keyboard("{Enter}");

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toBeInTheDocument();
      expect(alert).toHaveTextContent(/invalid email or password, or account disabled/i);
    });
  });

  it("reads invitation fragment once and erases it from the URL (Spec 7.1)", async () => {
    // Set window.location.hash with invitation token
    window.location.hash = "token=test-invitation-token-12345";
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");

    renderWithProviders(<RegisterForm />);

    expect(replaceStateSpy).toHaveBeenCalledWith(null, "", window.location.pathname);
    // Inputs are rendered because token was successfully extracted from fragment
    expect(screen.getByLabelText(/display name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password \(min 12 characters\)/i)).toBeInTheDocument();
  });

  it("shows invitation required notice when fragment is absent", () => {
    window.location.hash = "";
    renderWithProviders(<RegisterForm />);

    expect(screen.getByText(/invitation required/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/display name/i)).not.toBeInTheDocument();
  });
});

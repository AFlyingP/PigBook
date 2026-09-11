import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
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
  type User as UserSchema,
} from "../src/api/client";
import { AuthProvider } from "../src/features/auth/AuthContext";
import { LoginForm } from "../src/features/auth/LoginForm";
import { RegisterForm } from "../src/features/auth/RegisterForm";
import { RequireAuth } from "../src/components/RequireAuth";

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

  it("preserves attempt for matching principal regardless of age so UI enforces 24-hour rule (Spec 7.3)", () => {
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
    expect(restored).not.toBeNull();
    expect(restored?.key).toBe("stale-key-1");
    expect(restored?.createdAt).toBe(oldDate);

    // clearAttempt works directly
    beginAttempt(user, "blackout", {});
    expect(sessionStorage.getItem("commonsbook_create_attempt")).not.toBeNull();
    clearAttempt();
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("exhaustively verifies token responses never enter persistent frontend caches (R5, Spec 7.2)", () => {
    const testToken = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.doNotStoreThis";
    setAccessToken(testToken);

    // Ensure createAttempt draft is saved
    beginAttempt("user-1", "booking", { slot: 1 });

    // Exhaustive localStorage check: must be completely empty
    expect(localStorage.length).toBe(0);

    const jwtPattern = /^[A-Za-z0-9-_]+.[A-Za-z0-9-_]+.[A-Za-z0-9-_]+$/;

    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)!;
      const val = localStorage.getItem(key)!;
      expect(val).not.toContain(testToken);
      expect(val).not.toMatch(jwtPattern);
    }

    // Exhaustive sessionStorage check: only commonsbook_create_attempt allowed
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i)!;
      expect(key).toBe("commonsbook_create_attempt");
      const val = sessionStorage.getItem(key)!;
      expect(val).not.toContain(testToken);
      expect(val).not.toMatch(jwtPattern);
    }

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

describe("LoginForm & RegisterForm Component Behavior (Spec 7.1, 7.2, 8.1)", () => {
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
    window.location.hash = "token=test-invitation-token-12345";
    const replaceStateSpy = vi.spyOn(window.history, "replaceState");

    renderWithProviders(<RegisterForm />);

    expect(replaceStateSpy).toHaveBeenCalledWith(null, "", window.location.pathname);
    expect(screen.getByLabelText(/display name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Password \(/i)).toBeInTheDocument();
  });

  it("shows invitation required notice when fragment is absent", () => {
    window.location.hash = "";
    renderWithProviders(<RegisterForm />);

    expect(screen.getByText(/invitation required/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/display name/i)).not.toBeInTheDocument();
  });

  it("distinguishes 422 VALIDATION_ERROR from INVALID_INVITATION and parses real details.errors shape without [object Object] (R6, R9)", async () => {
    window.location.hash = "token=valid-token";
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 422,
      headers: new Headers(),
      json: async () => ({
        error: {
          code: "VALIDATION_ERROR",
          message: "Request validation failed",
          details: {
            errors: [
              {
                loc: ["body", "display_name"],
                msg: "String should have at least 1 character",
                type: "string_too_short",
              },
              {
                loc: ["body", "email"],
                msg: "value is not a valid email address",
                type: "value_error",
              },
            ],
          },
        },
      }),
    } as Response);

    renderWithProviders(<RegisterForm />);

    fireEvent.change(screen.getByLabelText(/email address/i), { target: { value: "test@example.com" } });
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Test User" } });
    fireEvent.change(screen.getByLabelText(/^Password \(/i), { target: { value: "ValidPassword123!" } });

    fireEvent.click(screen.getByRole("button", { name: /complete registration/i }));

    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert.textContent).not.toContain("[object Object]");
      expect(alert).toHaveTextContent(/display_name: String should have at least 1 character/i);
      expect(alert).toHaveTextContent(/email: value is not a valid email address/i);
      expect(alert).not.toHaveTextContent(/invalid or expired invitation token/i);
    });
  });

  it("enforces 12-character minimum counting Unicode codepoints (R6, Spec 8.1)", async () => {
    window.location.hash = "token=valid-token";
    renderWithProviders(<RegisterForm />);

    fireEvent.change(screen.getByLabelText(/email address/i), { target: { value: "test@example.com" } });
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Test User" } });

    // 11 emoji codepoints: each emoji is 2 UTF-16 code units (length=22) but only 11 codepoints (< 12)
    const elevenEmojis = "😀".repeat(11);
    expect(elevenEmojis.length).toBe(22); // code units
    expect([...elevenEmojis].length).toBe(11); // codepoints

    fireEvent.change(screen.getByLabelText(/^Password \(/i), {
      target: { value: elevenEmojis },
    });

    fireEvent.click(screen.getByRole("button", { name: /complete registration/i }));

    await waitFor(() => {
      expect(screen.getByText(/password must be at least 12 characters long/i)).toBeInTheDocument();
    });
  });
});

describe("RequireAuth Role-Guarded Access Control (R1, Spec 7.1, 7.2)", () => {
  function renderGuardedRoute(_user: UserSchema | null, adminOnly: boolean = false) {
    const qc = new QueryClient();
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/guarded"]}>
          <AuthProvider>
            <Routes>
              <Route element={<RequireAuth adminOnly={adminOnly} />}>
                <Route path="/guarded" element={<div>Guarded Content Allowed</div>} />
              </Route>
              <Route path="/login" element={<div>Login Page Redirect</div>} />
            </Routes>
          </AuthProvider>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("refuses unauthenticated users and redirects to login", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 401,
      headers: new Headers(),
      json: async () => ({ error: { code: "INVALID_REFRESH" } }),
    } as Response);

    renderGuardedRoute(null, false);

    expect(await screen.findByText("Login Page Redirect")).toBeInTheDocument();
    expect(screen.queryByText("Guarded Content Allowed")).not.toBeInTheDocument();
  });

  it("refuses non-admin users from adminOnly routes with 403 Forbidden alert (R1)", async () => {
    const memberUser: UserSchema = {
      id: "u-mem",
      email: "member@example.com",
      display_name: "Member User",
      role: "member",
      enabled: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          access_token: "jwt",
          token_type: "bearer",
          expires_in: 900,
          user: memberUser,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => memberUser,
      } as Response);

    renderGuardedRoute(memberUser, true);

    expect(await screen.findByText("403 Forbidden")).toBeInTheDocument();
    expect(screen.getByText(/administrator privileges are required/i)).toBeInTheDocument();
    expect(screen.queryByText("Guarded Content Allowed")).not.toBeInTheDocument();
  });

  it("admits admin users to adminOnly routes (R1)", async () => {
    const adminUser: UserSchema = {
      id: "u-adm",
      email: "admin@example.com",
      display_name: "Admin User",
      role: "admin",
      enabled: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          access_token: "jwt",
          token_type: "bearer",
          expires_in: 900,
          user: adminUser,
        }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => adminUser,
      } as Response);

    renderGuardedRoute(adminUser, true);

    expect(await screen.findByText("Guarded Content Allowed")).toBeInTheDocument();
    expect(screen.queryByText("403 Forbidden")).not.toBeInTheDocument();
  });
});

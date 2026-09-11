import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { AdminBookingsTable } from "../src/features/admin/operations/AdminBookingsTable";
import { AdminBookingCancelDialog } from "../src/features/admin/operations/AdminBookingCancelDialog";
import { UserTable } from "../src/features/admin/operations/UserTable";
import { InvitationDialog } from "../src/features/admin/operations/InvitationDialog";
import { AuditTable } from "../src/features/admin/operations/AuditTable";
import { OutboxTable } from "../src/features/admin/operations/OutboxTable";
import { RequireAuth } from "../src/components/RequireAuth";
import { AuthContext, type AuthContextType } from "../src/features/auth/AuthContext";
import type { components } from "../src/api/schema";

type User = components["schemas"]["User"];
type Booking = components["schemas"]["Booking"];
type Audit = components["schemas"]["Audit"];
type OutboxView = components["schemas"]["OutboxView"];

const mockAdminUser: User = {
  id: "admin-1111-1111-1111",
  email: "admin@example.com",
  display_name: "Admin User",
  role: "admin",
  enabled: true,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const mockMemberUser: User = {
  id: "member-2222-2222-2222",
  email: "member@example.com",
  display_name: "Member User",
  role: "member",
  enabled: true,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const mockBooking: Booking = {
  id: "booking-8888-8888-8888",
  resource_id: "55555555-5555-4555-8555-555555555555",
  user_id: mockMemberUser.id,
  kind: "reservation",
  starts_at: "2026-09-15T14:00:00Z",
  ends_at: "2026-09-15T16:00:00Z",
  status: "confirmed",
  version: 2,
  created_at: "2026-09-10T00:00:00Z",
  updated_at: "2026-09-10T00:00:00Z",
};

const mockAudit: Audit = {
  id: "audit-1",
  actor_id: mockAdminUser.id,
  action: "admin.user_patch",
  target_type: "user",
  target_id: mockMemberUser.id,
  request_id: "req-12345",
  details: { role: "admin", previous_role: "member" },
  created_at: "2026-09-10T12:00:00Z",
};

const mockDeadOutbox: OutboxView = {
  id: "outbox-1",
  event_type: "booking_confirmed",
  aggregate_id: mockBooking.id,
  status: "dead",
  attempts: 5,
  occurred_at: "2026-09-10T10:00:00Z",
  available_at: "2026-09-10T10:05:00Z",
  last_error: "Connection timeout to notification provider",
};

const mockPendingOutbox: OutboxView = {
  id: "outbox-2",
  event_type: "booking_cancelled",
  aggregate_id: mockBooking.id,
  status: "pending",
  attempts: 0,
  occurred_at: "2026-09-10T11:00:00Z",
  available_at: "2026-09-10T11:00:00Z",
  last_error: null,
};

function renderWithProviders(
  ui: React.ReactElement,
  {
    user = mockAdminUser,
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
    initialEntries = ["/"],
  }: { user?: User | null; queryClient?: QueryClient; initialEntries?: string[] } = {}
) {
  const authValue: AuthContextType = {
    user,
    isAuthenticated: Boolean(user),
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    checkSession: vi.fn(),
    refetchMe: vi.fn(),
  };

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue}>
        <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>
  );
}

describe("Admin Bookings (Spec 7.1, 8.3, 4.2 E24-E26)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("filters bookings by status and passes query parameters", async () => {
    let capturedUrl = "";
    vi.mocked(fetch).mockImplementation(async (url) => {
      capturedUrl = String(url);
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockBooking],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<AdminBookingsTable />);

    await waitFor(() => {
      expect(screen.getByText(/booking-.../i)).toBeInTheDocument();
    });

    // Change status filter
    const statusInput = document.querySelector("#booking-status-filter")?.parentElement?.querySelector("input");
    if (statusInput) {
      fireEvent.change(statusInput, { target: { value: "cancelled" } });
    }

    await waitFor(() => {
      expect(capturedUrl).toContain("status=cancelled");
    });
  });

  it("cancels booking with If-Match from current version and optional reason", async () => {
    let capturedHeaders: Headers | undefined;
    let capturedBody: Record<string, unknown> = {};

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      const u = String(url);
      if (u.includes("/cancel")) {
        capturedHeaders = new Headers(init?.headers);
        capturedBody = JSON.parse(String(init?.body || "{}"));
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"3"' }),
          json: async () => ({ ...mockBooking, status: "cancelled", version: 3 }),
        } as Response;
      }
      // Detail fetch
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"2"' }),
        json: async () => mockBooking,
      } as Response;
    });

    const onCancelled = vi.fn();
    renderWithProviders(
      <AdminBookingCancelDialog
        open={true}
        booking={mockBooking}
        onClose={vi.fn()}
        onCancelled={onCancelled}
      />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Cancellation Reason/i), {
      target: { value: "Facility maintenance required" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Confirm Cancellation/i }));

    await waitFor(() => {
      expect(capturedHeaders?.get("If-Match")).toBe('"2"');
      expect(capturedBody.reason).toBe("Facility maintenance required");
      expect(onCancelled).toHaveBeenCalled();
    });
  });

  it("handles 412 on cancel by refetching and warning user", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/cancel")) {
        return {
          ok: false,
          status: 412,
          headers: new Headers(),
          json: async () => ({
            error: { code: "VERSION_MISMATCH", message: "Stale version", details: {} },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"3"' }),
        json: async () => ({ ...mockBooking, version: 3 }),
      } as Response;
    });

    renderWithProviders(
      <AdminBookingCancelDialog
        open={true}
        booking={mockBooking}
        onClose={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Confirm Cancellation/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Another update occurred to this booking/i)
      ).toBeInTheDocument();
    });
  });

  it("surfaces 409 TOO_LATE as a clear terminal message", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/cancel")) {
        return {
          ok: false,
          status: 409,
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "TOO_LATE",
              message: "Cancellation deadline passed",
              details: {},
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"2"' }),
        json: async () => mockBooking,
      } as Response;
    });

    renderWithProviders(
      <AdminBookingCancelDialog
        open={true}
        booking={mockBooking}
        onClose={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Confirm Cancellation/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/cancellation deadline has passed/i)).toBeInTheDocument();
    });
  });
});

describe("Admin Users & Roles (Spec 7.1, 4.2 E27-E29)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("UserTable rejects LAST_ADMIN rejection accurately and keeps previous role visible", async () => {
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      if (init?.method === "PATCH") {
        return {
          ok: false,
          status: 409,
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "LAST_ADMIN",
              message: "Cannot modify the last active administrator",
              details: {},
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockAdminUser],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<UserTable />);

    await waitFor(() => {
      expect(screen.getByText("Admin User")).toBeInTheDocument();
      expect(screen.getByText("Administrator")).toBeInTheDocument();
    });

    // Try demoting the last admin
    const demoteBtn = screen.getByRole("button", { name: /Change role for Admin User/i });
    fireEvent.click(demoteBtn);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Cannot demote the last active administrator/i)).toBeInTheDocument();
      // Previous role remains Administrator
      expect(screen.getByText("Administrator")).toBeInTheDocument();
    });
  });

  it("handles stale user 412 by refetching and warning user", async () => {
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      if (init?.method === "PATCH") {
        return {
          ok: false,
          status: 412,
          headers: new Headers(),
          json: async () => ({
            error: { code: "VERSION_MISMATCH", message: "Stale version", details: {} },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockMemberUser],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<UserTable />);

    await waitFor(() => {
      expect(screen.getByText("Member User")).toBeInTheDocument();
    });

    const disableBtn = screen.getByRole("button", { name: /Disable Member User/i });
    fireEvent.click(disableBtn);

    await waitFor(() => {
      expect(
        screen.getByText(/Another update occurred to this user account/i)
      ).toBeInTheDocument();
    });
  });

  it("generates copy-once invitation link, clears link on close, and never persists link", async () => {
    sessionStorage.clear();
    localStorage.clear();

    const mockInviteResult = {
      id: "inv-1",
      email: "newmember@example.com",
      role: "member",
      expires_at: "2026-09-18T00:00:00Z",
      invitation_url: "http://localhost:5173/register#token=secret-token-12345",
    };

    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: true,
        status: 201,
        headers: new Headers({ "Cache-Control": "no-store" }),
        json: async () => mockInviteResult,
      } as Response;
    });

    const onClose = vi.fn();
    const { unmount } = renderWithProviders(
      <InvitationDialog open={true} onClose={onClose} />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Email Address/i), {
      target: { value: "newmember@example.com" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Generate Invitation/i }));

    await waitFor(() => {
      expect(screen.getByText("Invitation Link Generated")).toBeInTheDocument();
      expect(screen.getByDisplayValue(mockInviteResult.invitation_url)).toBeInTheDocument();
    });

    // Guardrail: Link MUST NEVER be persisted in storage
    expect(sessionStorage.getItem("commonsbook_invitation_url")).toBeNull();
    expect(localStorage.getItem("commonsbook_invitation_url")).toBeNull();
    expect(JSON.stringify(sessionStorage)).not.toContain("secret-token-12345");
    expect(JSON.stringify(localStorage)).not.toContain("secret-token-12345");

    // Close dialog
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalled();

    unmount();
    // After unmount / close, verify storage still has no secret
    expect(JSON.stringify(sessionStorage)).not.toContain("secret-token-12345");
  });
});

describe("Admin Operations — Audit & Outbox (Spec 7.1, 8.3, 4.2 E30-E32)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("AuditTable renders audit entries and allows target_id filtering", async () => {
    let capturedUrl = "";
    vi.mocked(fetch).mockImplementation(async (url) => {
      capturedUrl = String(url);
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockAudit],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<AuditTable />);

    await waitFor(() => {
      expect(screen.getByText("System Audit Log")).toBeInTheDocument();
      expect(screen.getByText("admin.user_patch")).toBeInTheDocument();
      expect(screen.getByText(/req-12345/i)).toBeInTheDocument();
    });

    // Filter by target ID
    fireEvent.change(screen.getByLabelText(/Filter by Target ID/i), {
      target: { value: mockMemberUser.id },
    });
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));

    await waitFor(() => {
      expect(capturedUrl).toContain(`target_id=${mockMemberUser.id}`);
    });
  });

  it("OutboxTable enables retry ONLY for dead items and handles confirmation and 409 INVALID_STATE", async () => {
    let retryCalled = false;
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/retry")) {
        retryCalled = true;
        return {
          ok: false,
          status: 409,
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "INVALID_STATE",
              message: "Only dead outbox events can be retried",
              details: {},
            },
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockDeadOutbox, mockPendingOutbox],
          total: 2,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<OutboxTable />);

    await waitFor(() => {
      expect(screen.getByText("Transactional Outbox Queue")).toBeInTheDocument();
      expect(screen.getByText("Dead")).toBeInTheDocument();
      expect(screen.getByText("Pending")).toBeInTheDocument();
    });

    // Dead event button should be ENABLED
    const retryButtons = screen.getAllByRole("button", { name: /Retry/i });
    const deadRetryBtn = retryButtons.find((btn) => !btn.hasAttribute("disabled"));
    expect(deadRetryBtn).toBeDefined();

    // Pending event button should be DISABLED
    const disabledRetryBtn = retryButtons.find((btn) => btn.hasAttribute("disabled"));
    expect(disabledRetryBtn).toBeDefined();

    // Click retry on dead event -> opens confirmation modal
    fireEvent.click(deadRetryBtn!);

    await waitFor(() => {
      expect(screen.getByText("Retry Dead Outbox Event")).toBeInTheDocument();
      expect(screen.getByText(/Are you sure you want to retry event/i)).toBeInTheDocument();
    });

    // Confirm retry -> triggers POST and receives 409 INVALID_STATE
    const confirmBtn = screen.getByRole("button", { name: /Confirm Retry/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(retryCalled).toBe(true);
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/no longer in dead state/i)).toBeInTheDocument();
    });
  });
});

describe("Member Access Denial (Spec 7.1, 8.1, US-02)", () => {
  it("signed-in member reaching administrator route sees accessible 403 and receives no admin data", async () => {
    renderWithProviders(
      <Routes>
        <Route element={<RequireAuth adminOnly />}>
          <Route path="/admin/resources" element={<div>ADMIN SECRET DATA</div>} />
        </Route>
      </Routes>,
      {
        user: mockMemberUser, // member role
        initialEntries: ["/admin/resources"],
      }
    );

    // Member must see accessible 403 Forbidden
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("403 Forbidden")).toBeInTheDocument();
    expect(
      screen.getByText(/Administrator privileges are required/i)
    ).toBeInTheDocument();

    // Prove child route never rendered and secret data is absent
    expect(screen.queryByText("ADMIN SECRET DATA")).not.toBeInTheDocument();
  });
});

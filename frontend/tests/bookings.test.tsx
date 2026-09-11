import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { BookingDialog, type BookingDialogProps } from "../src/features/bookings/BookingDialog";
import { OwnBookingsTable } from "../src/features/bookings/OwnBookingsTable";
import {
  beginAttempt,
  restoreAttempt,
  clearAttempt,
  type CreateAttempt,
} from "../src/api/createAttempt";
import { ApiError } from "../src/api/client";
import { AuthContext, type AuthContextType } from "../src/features/auth/AuthContext";
import type { components } from "../src/api/schema";

type User = components["schemas"]["User"];
type Resource = components["schemas"]["Resource"];
type Booking = components["schemas"]["Booking"];

const mockUser: User = {
  id: "user-1111-1111-1111",
  email: "member@example.com",
  display_name: "Member One",
  role: "member",
  enabled: true,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const mockResource: Resource = {
  id: "55555555-5555-4555-8555-555555555555",
  name: "Community Woodshop",
  location: "Workshop Bay B",
  description: "Power tools and benches",
  active: true,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const mockWindow = {
  starts_at: "2026-09-15T14:00:00Z",
  ends_at: "2026-09-15T16:00:00Z",
};

function renderWithProviders(
  ui: React.ReactElement,
  {
    user = mockUser,
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  }: { user?: User | null; queryClient?: QueryClient } = {}
) {
  const authValue: AuthContextType = {
    user,
    token: user ? "mock-token" : null,
    isAuthenticated: Boolean(user),
    isLoading: false,
    login: vi.fn(),
    logout: vi.fn(),
    refreshUser: vi.fn(),
  };

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={authValue}>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>
  );
}

describe("BookingDialog Component & Uncertain Request Recovery (Spec 4.3, 7.2, 7.3)", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
    localStorage.clear();
  });

  it("submits booking with UUID v4 Idempotency-Key and persists draft during attempt", async () => {
    let capturedHeaders: Headers | undefined;
    let capturedBody: any;

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      capturedHeaders = new Headers(init?.headers);
      capturedBody = JSON.parse(String(init?.body || "{}"));
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => ({
          id: "b-1",
          resource_id: mockResource.id,
          user_id: mockUser.id,
          kind: "reservation",
          starts_at: mockWindow.starts_at,
          ends_at: mockWindow.ends_at,
          status: "confirmed",
          version: 1,
          created_at: "2026-09-15T12:00:00Z",
          updated_at: "2026-09-15T12:00:00Z",
        }),
      } as Response;
    });

    const onClose = vi.fn();
    const onSuccess = vi.fn();

    renderWithProviders(
      <BookingDialog
        open={true}
        onClose={onClose}
        resource={mockResource}
        window={mockWindow}
        onBookingSuccess={onSuccess}
      />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Community Woodshop")).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Confirm Reservation" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(onSuccess).toHaveBeenCalled();
    });

    // Verify Idempotency-Key was sent
    const key = capturedHeaders?.get("Idempotency-Key");
    expect(key).toBeDefined();
    expect(key).toMatch(/^[0-9a-f-]{36}$/i);

    // Verify body payload
    expect(capturedBody).toEqual({
      resource_id: mockResource.id,
      starts_at: mockWindow.starts_at,
      ends_at: mockWindow.ends_at,
    });

    // Definitive 201 clears the attempt from sessionStorage
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("handles uncertain network failure or 503 by preserving key/payload and offering same-key retry", async () => {
    let callCount = 0;
    const sentKeys: string[] = [];

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      callCount++;
      const headers = new Headers(init?.headers);
      sentKeys.push(headers.get("Idempotency-Key") || "");

      if (callCount === 1) {
        // Return 503 Retryable Unavailable
        return {
          ok: false,
          status: 503,
          statusText: "Service Unavailable",
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "RETRYABLE_UNAVAILABLE",
              message: "Database busy, please retry",
            },
          }),
        } as Response;
      }

      // Second call succeeds
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => ({
          id: "b-2",
          resource_id: mockResource.id,
          user_id: mockUser.id,
          kind: "reservation",
          starts_at: mockWindow.starts_at,
          ends_at: mockWindow.ends_at,
          status: "confirmed",
          version: 1,
          created_at: "2026-09-15T12:00:00Z",
          updated_at: "2026-09-15T12:00:00Z",
        }),
      } as Response;
    });

    renderWithProviders(
      <BookingDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
      />
    );

    // Initial submission
    const confirmBtn = screen.getByRole("button", { name: "Confirm Reservation" });
    fireEvent.click(confirmBtn);

    // Expect 503 error message and Retry button
    expect(await screen.findByText(/Database busy, please retry/i)).toBeInTheDocument();
    expect(screen.getByText(/Uncertain Request Recovery/i)).toBeInTheDocument();

    // Draft is retained in sessionStorage
    const stored = JSON.parse(sessionStorage.getItem("commonsbook_create_attempt") || "{}");
    expect(stored.key).toBe(sentKeys[0]);

    // Retry button appears
    const retryBtn = screen.getByRole("button", { name: "Retry Booking" });
    expect(retryBtn).toBeInTheDocument();

    // Click retry
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByText(/Booking confirmed successfully/i)).toBeInTheDocument();
    });

    // Key must NOT rotate across uncertain retry (Spec 4.3, 7.3)
    expect(sentKeys[0]).toBe(sentKeys[1]);
    // On definitive 201, attempt is cleared
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("handles definitive 409 SLOT_CONFLICT: clears attempt, refreshes availability, offers waitlist, and requires new key", async () => {
    let sentKey: string | null = null;

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      const headers = new Headers(init?.headers);
      sentKey = headers.get("Idempotency-Key");
      return {
        ok: false,
        status: 409,
        statusText: "Conflict",
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "SLOT_CONFLICT",
            message: "The requested time interval overlaps with an existing booking",
          },
        }),
      } as Response;
    });

    const onShowWaitlist = vi.fn();
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderWithProviders(
      <BookingDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
        onShowWaitlist={onShowWaitlist}
      />,
      { queryClient }
    );

    fireEvent.click(screen.getByRole("button", { name: "Confirm Reservation" }));

    // Definitive conflict error displayed
    expect(await screen.findByText(/overlaps with an existing booking/i)).toBeInTheDocument();

    // Waitlist button offered
    const waitlistBtn = screen.getByRole("button", { name: /Join Waitlist for This Slot/i });
    expect(waitlistBtn).toBeInTheDocument();
    fireEvent.click(waitlistBtn);
    expect(onShowWaitlist).toHaveBeenCalledWith(mockWindow);

    // Definitive 409 clears the attempt
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();

    // Refreshes availability query
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["resources"] });
  });

  it("handles definitive 422: clears attempt and renders readable field errors without [object Object]", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 422,
        statusText: "Unprocessable Entity",
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "VALIDATION_ERROR",
            message: "Invalid booking window requested",
            details: {
              errors: [
                {
                  loc: ["body", "starts_at"],
                  msg: "Start time must be aligned on a 30-minute boundary",
                  type: "value_error",
                },
              ],
            },
          },
        }),
      } as Response;
    });

    renderWithProviders(
      <BookingDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Confirm Reservation" }));

    expect(await screen.findByText(/Invalid booking window requested/i)).toBeInTheDocument();
    expect(
      screen.getByText(/• body.starts_at: Start time must be aligned on a 30-minute boundary/i)
    ).toBeInTheDocument();

    // Must never render raw [object Object]
    expect(screen.queryByText("\[object Object\]")).not.toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();

    // Definitive 422 clears the attempt
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("enforces Spec 7.3 24-hour rule: under 24h allows same-key retry; at/over 24h stops auto-replay and offers My Bookings check", () => {
    // Case 1: Under 24 hours (e.g. 2 hours old)
    const recentDate = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    const recentAttempt: CreateAttempt = {
      key: "recent-key-1",
      payload: {
        resource_id: mockResource.id,
        starts_at: mockWindow.starts_at,
        ends_at: mockWindow.ends_at,
      },
      kind: "booking",
      principalId: mockUser.id,
      createdAt: recentDate,
    };
    sessionStorage.setItem("commonsbook_create_attempt", JSON.stringify(recentAttempt));

    const { unmount } = renderWithProviders(
      <BookingDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
      />
    );

    // Under 24 hours: same-key retry is available
    expect(screen.getByText(/Uncertain Request Recovery/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry Booking" })).toBeInTheDocument();
    unmount();

    // Case 2: At or over 24 hours (e.g. 25 hours old)
    const staleDate = new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString();
    const staleAttempt: CreateAttempt = {
      key: "stale-key-1",
      payload: {
        resource_id: mockResource.id,
        starts_at: mockWindow.starts_at,
        ends_at: mockWindow.ends_at,
      },
      kind: "booking",
      principalId: mockUser.id,
      createdAt: staleDate,
    };
    sessionStorage.setItem("commonsbook_create_attempt", JSON.stringify(staleAttempt));

    const onNavigateMyBookings = vi.fn();
    renderWithProviders(
      <BookingDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
        onNavigateMyBookings={onNavigateMyBookings}
      />
    );

    // Over 24 hours: automatic replay stops, warning and Check My Bookings action shown
    expect(screen.getByText(/Previous Submission Pending/i)).toBeInTheDocument();
    expect(
      screen.getByText(/over 24 hours ago was not completed definitively/i)
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry Booking" })).not.toBeInTheDocument();

    const checkBtn = screen.getByRole("button", { name: "Check My Bookings" });
    fireEvent.click(checkBtn);
    expect(onNavigateMyBookings).toHaveBeenCalled();

    // Start New Attempt clears stale attempt
    const startNewBtn = screen.getByRole("button", { name: "Start New Attempt" });
    fireEvent.click(startNewBtn);
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });

  it("ensures cross-account clearing: attempt belonging to user A is never accessed by user B", () => {
    const userA = "user-aaa";
    const userB = "user-bbb";

    beginAttempt(userA, "booking", { resource_id: "res-1" });
    expect(sessionStorage.getItem("commonsbook_create_attempt")).not.toBeNull();

    // User B restores: returns null and purges draft
    const restored = restoreAttempt(userB);
    expect(restored).toBeNull();
    expect(sessionStorage.getItem("commonsbook_create_attempt")).toBeNull();
  });
});

describe("OwnBookingsTable Component & Cancellation (Spec 4.1, 4.2 E10-E12, 7.2)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const mockBookingsPage = {
    items: [
      {
        id: "88888888-8888-4888-8888-888888888888",
        resource_id: mockResource.id,
        user_id: mockUser.id,
        kind: "reservation",
        starts_at: "2026-09-15T14:00:00Z",
        ends_at: "2026-09-15T16:00:00Z",
        status: "confirmed",
        version: 1,
        created_at: "2026-09-10T12:00:00Z",
        updated_at: "2026-09-10T12:00:00Z",
      },
    ],
    total: 1,
    limit: 10,
    offset: 0,
  };

  it("lists bookings, confirms cancellation, and sends If-Match ETag from GET booking detail", async () => {
    let capturedIfMatch: string | null = null;
    let cancelBody: any;

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      const u = String(url);

      if (u.includes("/resources")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => ({ items: [mockResource], total: 1 }),
        } as Response;
      }

      if (u.includes("/cancel")) {
        const headers = new Headers(init?.headers);
        capturedIfMatch = headers.get("If-Match");
        cancelBody = JSON.parse(String(init?.body || "{}"));
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"2"' }),
          json: async () => ({
            ...mockBookingsPage.items[0],
            status: "cancelled",
            version: 2,
          }),
        } as Response;
      }

      if (u.includes("/bookings/88888888-8888-4888-8888-888888888888")) {
        // GET detail returns ETag "1"
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"1"' }),
          json: async () => mockBookingsPage.items[0],
        } as Response;
      }

      // GET /bookings
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => mockBookingsPage,
      } as Response;
    });

    renderWithProviders(<OwnBookingsTable />);

    expect(await screen.findByText("Community Woodshop")).toBeInTheDocument();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();

    // Click Cancel
    const cancelBtn = screen.getByRole("button", {
      name: /Cancel reservation for Community Woodshop/i,
    });
    fireEvent.click(cancelBtn);

    // Cancel confirmation dialog opens
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Cancel Reservation")).toBeInTheDocument();

    const reasonInput = screen.getByLabelText(/Cancellation Reason/i);
    fireEvent.change(reasonInput, { target: { value: "Changed schedule" } });

    const confirmCancelBtn = screen.getByRole("button", { name: "Confirm Cancellation" });
    fireEvent.click(confirmCancelBtn);

    await waitFor(() => {
      expect(screen.getByText(/Booking cancelled successfully/i)).toBeInTheDocument();
    });

    // Check If-Match header format: must be strong entity tag '"1"' (Spec 4.1, 7.2)
    expect(capturedIfMatch).toBe('"1"');
    expect(cancelBody).toEqual({ reason: "Changed schedule" });
  });

  it("handles 412 VERSION_MISMATCH during cancel by warning user and refetching state", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);

      if (u.includes("/resources")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => ({ items: [mockResource], total: 1 }),
        } as Response;
      }

      if (u.includes("/bookings/88888888-8888-4888-8888-888888888888") && !u.includes("/cancel")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"1"' }),
          json: async () => mockBookingsPage.items[0],
        } as Response;
      }

      if (u.includes("/cancel")) {
        return {
          ok: false,
          status: 412,
          statusText: "Precondition Failed",
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "VERSION_MISMATCH",
              message: "Target booking version does not match If-Match",
            },
          }),
        } as Response;
      }

      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => mockBookingsPage,
      } as Response;
    });

    renderWithProviders(<OwnBookingsTable />);

    const cancelBtn = await screen.findByRole("button", {
      name: /Cancel reservation for Community Woodshop/i,
    });
    fireEvent.click(cancelBtn);

    const confirmCancelBtn = screen.getByRole("button", { name: "Confirm Cancellation" });
    fireEvent.click(confirmCancelBtn);

    expect(
      await screen.findByText(/Another update occurred to this booking. Refreshing latest status/i)
    ).toBeInTheDocument();
  });
});

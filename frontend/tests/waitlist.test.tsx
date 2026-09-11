import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { WaitlistDialog } from "../src/features/waitlist/WaitlistDialog";
import { OfferCard } from "../src/features/waitlist/OfferCard";
import { OwnWaitlistTable } from "../src/features/waitlist/OwnWaitlistTable";
import { AuthContext, type AuthContextType } from "../src/features/auth/AuthContext";
import type { components } from "../src/api/schema";

type User = components["schemas"]["User"];
type Resource = components["schemas"]["Resource"];
type WaitEntry = components["schemas"]["WaitEntry"];
type Booking = components["schemas"]["Booking"];

const mockUser: User = {
  id: "user-2222-2222-2222",
  email: "member2@example.com",
  display_name: "Member Two",
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
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>
  );
}

describe("WaitlistDialog Component (Spec 7.1, 7.2, 7.3)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("joins waitlist via POST /api/v1/waitlist and invalidates waitlist and resource queries on 201", async () => {
    let capturedBody: Record<string, unknown> | undefined;

    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      capturedBody = JSON.parse(String(init?.body || "{}"));
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => ({
          id: "w-1",
          user_id: mockUser.id,
          resource_id: mockResource.id,
          starts_at: mockWindow.starts_at,
          ends_at: mockWindow.ends_at,
          status: "waiting",
          offered_booking_id: null,
          version: 1,
          created_at: "2026-09-15T10:00:00Z",
          updated_at: "2026-09-15T10:00:00Z",
        }),
      } as Response;
    });

    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const onJoinSuccess = vi.fn();

    renderWithProviders(
      <WaitlistDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
        onJoinSuccess={onJoinSuccess}
      />,
      { queryClient }
    );

    fireEvent.click(screen.getByRole("button", { name: "Join Waitlist" }));

    expect(await screen.findByText(/You have joined the waitlist for this slot/i)).toBeInTheDocument();
    expect(onJoinSuccess).toHaveBeenCalled();

    expect(capturedBody).toEqual({
      resource_id: mockResource.id,
      starts_at: mockWindow.starts_at,
      ends_at: mockWindow.ends_at,
    });

    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["waitlist", "mine"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["resources"] });
  });

  it("handles 409 SLOT_AVAILABLE: refreshes availability and offers booking action", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 409,
        statusText: "Conflict",
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "SLOT_AVAILABLE",
            message: "Slot is currently available for direct booking",
          },
        }),
      } as Response;
    });

    const onShowBooking = vi.fn();
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderWithProviders(
      <WaitlistDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
        onShowBooking={onShowBooking}
      />,
      { queryClient }
    );

    fireEvent.click(screen.getByRole("button", { name: "Join Waitlist" }));

    expect(await screen.findByText(/This slot is currently available for direct booking/i)).toBeInTheDocument();
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["resources"] });

    const bookBtn = screen.getByRole("button", { name: "Book This Slot Now" });
    fireEvent.click(bookBtn);
    expect(onShowBooking).toHaveBeenCalledWith(mockWindow);
  });

  it("handles 409 WAITLIST_FULL as capacity error with no claim of having joined (Spec 5.3, 7.3)", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 409,
        statusText: "Conflict",
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "WAITLIST_FULL",
            message: "Waitlist has reached maximum capacity of 500 entries",
          },
        }),
      } as Response;
    });

    renderWithProviders(
      <WaitlistDialog
        open={true}
        onClose={vi.fn()}
        resource={mockResource}
        window={mockWindow}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "Join Waitlist" }));

    expect(
      await screen.findByText(/waitlist for this resource is currently full/i)
    ).toBeInTheDocument();

    // Must not claim user joined
    expect(screen.queryByText(/joined/i)).not.toBeInTheDocument();
  });
});

describe("OfferCard Component & Hold Acceptance/Countdown (Spec 7.1, 7.2, 7.3, Revision 1.1)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  const mockOfferedEntry: WaitEntry = {
    id: "wait-entry-9999",
    user_id: mockUser.id,
    resource_id: mockResource.id,
    starts_at: "2026-09-15T14:00:00Z",
    ends_at: "2026-09-15T16:00:00Z",
    status: "offered",
    offered_booking_id: "booking-hold-7777",
    version: 3, // Entry version is 3
    created_at: "2026-09-14T10:00:00Z",
    updated_at: "2026-09-15T12:00:00Z",
  };

  const mockHoldBooking: Booking = {
    id: "booking-hold-7777",
    resource_id: mockResource.id,
    user_id: mockUser.id,
    kind: "reservation",
    starts_at: "2026-09-15T14:00:00Z",
    ends_at: "2026-09-15T16:00:00Z",
    status: "offered",
    expires_at: new Date(Date.now() + 600 * 1000).toISOString(), // 10 minutes in future
    version: 7, // Booking version is 7 (DIFFERENT from entry version 3!)
    created_at: "2026-09-15T12:00:00Z",
    updated_at: "2026-09-15T12:00:00Z",
  };

  it("fetches offered_booking_id via E11 to obtain server expires_at and renders countdown", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/bookings/booking-hold-7777")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"7"' }),
          json: async () => mockHoldBooking,
        } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });

    renderWithProviders(
      <OfferCard entry={mockOfferedEntry} resourceName="Community Woodshop" />
    );

    // E11 was fetched using offered_booking_id
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith(
        expect.stringContaining("/api/v1/bookings/booking-hold-7777"),
        expect.anything()
      );
    });

    // Countdown timer rendered (between 09:55 and 10:00)
    expect(await screen.findByText(/09:5|10:00/)).toBeInTheDocument();
  });

  it("accepts offer using waitlist-entry version in If-Match (NOT booking version) (Spec 4.2 E16, 7.3)", async () => {
    let capturedIfMatch: string | null = null;
    let acceptBody: Record<string, unknown> | undefined;

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      const u = String(url);
      if (u.includes("/bookings/booking-hold-7777")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"7"' }),
          json: async () => mockHoldBooking,
        } as Response;
      }

      if (u.includes("/accept")) {
        const headers = new Headers(init?.headers);
        capturedIfMatch = headers.get("If-Match");
        acceptBody = JSON.parse(String(init?.body || "{}"));

        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"8"' }),
          json: async () => ({
            ...mockHoldBooking,
            status: "confirmed",
            version: 8,
          }),
        } as Response;
      }

      return { ok: false, status: 404 } as Response;
    });

    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const onActionSuccess = vi.fn();

    renderWithProviders(
      <OfferCard
        entry={mockOfferedEntry}
        resourceName="Community Woodshop"
        onActionSuccess={onActionSuccess}
      />,
      { queryClient }
    );

    const acceptBtn = await screen.findByRole("button", { name: "Accept waitlist offer" });
    fireEvent.click(acceptBtn);

    expect(await screen.findByText(/Offer accepted! Your reservation is now confirmed/i)).toBeInTheDocument();
    expect(onActionSuccess).toHaveBeenCalled();

    // Critical Revision 1.1 assertion:
    // If-Match must equal '"3"' (the ENTRY version), NOT '"7"' (the booking version)!
    expect(capturedIfMatch).toBe('"3"');
    expect(acceptBody).toEqual({});

    // Invalidates queries
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["waitlist", "mine"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["bookings", "mine"] });
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["resources"] });
  });

  it("refetches server state when countdown reaches 0 without assuming local terminal state (Spec 7.2, 7.3)", async () => {
    // Hold booking already at or past expiry deadline
    const expiringHold: Booking = {
      ...mockHoldBooking,
      expires_at: new Date(Date.now() - 500).toISOString(),
    };

    let fetchCount = 0;
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/bookings/booking-hold-7777")) {
        fetchCount++;
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"7"' }),
          json: async () => expiringHold,
        } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });

    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    renderWithProviders(
      <OfferCard entry={mockOfferedEntry} resourceName="Community Woodshop" />,
      { queryClient }
    );

    expect(await screen.findByText("00:00")).toBeInTheDocument();

    // Reaching deadline triggers query invalidations and server refetch without local state assumption
    await waitFor(
      () => {
        expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["waitlist", "mine"] });
        expect(invalidateSpy).toHaveBeenCalledWith({
          queryKey: ["bookings", "booking-hold-7777"],
        });
      },
      { timeout: 3000 }
    );

    // Refetched from server
    expect(fetchCount).toBeGreaterThanOrEqual(1);
  });

  it("restores focus to stable element upon successful offer acceptance and decline (Spec 7.2, R6)", async () => {
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/bookings/booking-hold-7777")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"7"' }),
          json: async () => mockHoldBooking,
        } as Response;
      }
      if (u.includes("/accept")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"8"' }),
          json: async () => ({ ...mockHoldBooking, status: "confirmed" }),
        } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });

    renderWithProviders(
      <div>
        <h1 id="my-waitlist-heading" tabIndex={-1}>
          My Waitlist
        </h1>
        <OfferCard entry={mockOfferedEntry} resourceName="Community Woodshop" />
      </div>
    );

    const acceptBtn = await screen.findByRole("button", { name: "Accept waitlist offer" });
    fireEvent.click(acceptBtn);

    await waitFor(() => {
      const heading = document.getElementById("my-waitlist-heading");
      expect(document.activeElement).toBe(heading);
    });
  });
});

describe("OwnWaitlistTable Component (Spec 7.1, 7.2)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists waitlist entries and withdraws with entry version in If-Match", async () => {
    let capturedIfMatch: string | null = null;

    const mockEntries: WaitEntry[] = [
      {
        id: "wait-1",
        user_id: mockUser.id,
        resource_id: mockResource.id,
        starts_at: "2026-09-16T14:00:00Z",
        ends_at: "2026-09-16T16:00:00Z",
        status: "waiting",
        offered_booking_id: null,
        version: 2,
        created_at: "2026-09-10T12:00:00Z",
        updated_at: "2026-09-10T12:00:00Z",
      },
    ];

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

      if (u.includes("/waitlist/wait-1") && init?.method === "DELETE") {
        capturedIfMatch = new Headers(init.headers).get("If-Match");
        return {
          ok: true,
          status: 200,
          headers: new Headers({ ETag: '"3"' }),
          json: async () => ({
            ...mockEntries[0],
            status: "cancelled",
            version: 3,
          }),
        } as Response;
      }

      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({ items: mockEntries, total: 1 }),
      } as Response;
    });

    renderWithProviders(<OwnWaitlistTable />);

    expect(await screen.findByText("Community Woodshop")).toBeInTheDocument();
    expect(screen.getByText("Waiting")).toBeInTheDocument();

    const leaveBtn = screen.getByRole("button", {
      name: /Leave waitlist for Community Woodshop/i,
    });
    fireEvent.click(leaveBtn);

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Leave Waitlist" })).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Confirm Leave" });
    fireEvent.click(confirmBtn);

    expect(await screen.findByText(/You have been removed from the waitlist/i)).toBeInTheDocument();
    // Must use entry version '"2"'
    expect(capturedIfMatch).toBe('"2"');
  });
});

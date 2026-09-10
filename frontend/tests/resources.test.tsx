import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import {
  getNewYorkOffsetString,
  getNewYorkMidnightUtc,
  getNewYorkInstantUtc,
  getSevenDayWindow,
  validateBookingWindow,
  checkSlotOccupancy,
} from "../src/features/resources/timeUtils";
import { ResourceList } from "../src/features/resources/ResourceList";
import { ResourceDetail, type BookingLaunchContract } from "../src/features/resources/ResourceDetail";

describe("Time & Timezone Utilities across DST Transitions (R2, Spec 1.2, 7.1)", () => {
  it("displays timezone America/New_York with visible offset", () => {
    const offset = getNewYorkOffsetString();
    expect(offset).toMatch(/^UTC[+-]\d{2}:\d{2}$/);
  });

  it("computes accurate UTC midnight boundaries across EDT and EST", () => {
    // Summer date in EDT (UTC-4) -> midnight is 04:00 UTC
    const summer = getNewYorkMidnightUtc("2026-07-15");
    expect(summer).toBe("2026-07-15T04:00:00.000Z");

    // Winter date in EST (UTC-5) -> midnight is 05:00 UTC
    const winter = getNewYorkMidnightUtc("2026-01-15");
    expect(winter).toBe("2026-01-15T05:00:00.000Z");
  });

  it("computes exact DST-aware UTC instants on spring-forward transition day (2026-03-08) (R2)", () => {
    // 2026-03-08: Clocks spring forward at 02:00 EST (UTC-5) to 03:00 EDT (UTC-4)
    // Boundary slot before transition: 01:30 local is EST (UTC-5) -> 06:30Z
    const boundaryPre = getNewYorkInstantUtc("2026-03-08", 1, 30);
    expect(boundaryPre).toBe("2026-03-08T06:30:00.000Z");

    // Morning slot after transition: 08:00 local is EDT (UTC-4) -> 12:00Z
    const morning = getNewYorkInstantUtc("2026-03-08", 8, 0);
    expect(morning).toBe("2026-03-08T12:00:00.000Z");

    // Afternoon slot after transition: 14:00 local is EDT (UTC-4) -> 18:00Z
    const afternoon = getNewYorkInstantUtc("2026-03-08", 14, 0);
    expect(afternoon).toBe("2026-03-08T18:00:00.000Z");
  });

  it("computes exact DST-aware UTC instants on fall-back transition day (2026-11-01) (R2)", () => {
    // 2026-11-01: Clocks fall back at 02:00 EDT (UTC-4) to 01:00 EST (UTC-5)
    // Boundary slot before transition: 01:30 local is EDT (UTC-4) -> 05:30Z
    const boundaryPre = getNewYorkInstantUtc("2026-11-01", 1, 30);
    expect(boundaryPre).toBe("2026-11-01T05:30:00.000Z");

    // Morning slot after transition: 08:00 local is EST (UTC-5) -> 13:00Z
    const morning = getNewYorkInstantUtc("2026-11-01", 8, 0);
    expect(morning).toBe("2026-11-01T13:00:00.000Z");

    // Afternoon slot after transition: 14:00 local is EST (UTC-5) -> 19:00Z
    const afternoon = getNewYorkInstantUtc("2026-11-01", 14, 0);
    expect(afternoon).toBe("2026-11-01T19:00:00.000Z");
  });

  it("computes 7-day schedule window bounded to <= 7 days duration across DST", () => {
    const window = getSevenDayWindow("2026-09-10");
    expect(window.days).toHaveLength(7);
    expect(window.starts_at).toBe("2026-09-10T04:00:00.000Z");

    const startMs = new Date(window.starts_at).getTime();
    const endMs = new Date(window.ends_at).getTime();
    const durationHours = (endMs - startMs) / (1000 * 60 * 60);

    expect(durationHours).toBeLessThanOrEqual(168);
    expect(durationHours).toBeGreaterThanOrEqual(167);
  });

  it("validates 30-minute alignment, duration, lead time and horizon (Spec 1.2)", () => {
    const baseNow = new Date("2026-09-10T12:00:00Z");

    // Valid 1-hour slot aligned on 30-minute boundary 2 hours in future
    const valid = validateBookingWindow(
      "2026-09-10T14:00:00Z",
      "2026-09-10T15:00:00Z",
      baseNow
    );
    expect(valid.valid).toBe(true);

    // Unaligned minutes (e.g. 15 minutes)
    const unaligned = validateBookingWindow(
      "2026-09-10T14:15:00Z",
      "2026-09-10T15:00:00Z",
      baseNow
    );
    expect(unaligned.valid).toBe(false);
    expect(unaligned.error).toMatch(/30-minute boundary/i);

    // Duration too short (< 30 minutes)
    const tooShort = validateBookingWindow(
      "2026-09-10T14:00:00Z",
      "2026-09-10T14:00:00Z",
      baseNow
    );
    expect(tooShort.valid).toBe(false);

    // Duration too long (> 4 hours)
    const tooLong = validateBookingWindow(
      "2026-09-10T14:00:00Z",
      "2026-09-10T19:00:00Z",
      baseNow
    );
    expect(tooLong.valid).toBe(false);
    expect(tooLong.error).toMatch(/between 30 minutes and 4 hours/i);

    // Lead time violation (< 15 minutes in future)
    const tooSoon = validateBookingWindow(
      "2026-09-10T12:00:00Z",
      "2026-09-10T13:00:00Z",
      baseNow
    );
    expect(tooSoon.valid).toBe(false);
    expect(tooSoon.error).toMatch(/at least 15 minutes in the future/i);
  });

  it("correctly evaluates half-open interval occupancy", () => {
    const occupied = [
      {
        starts_at: "2026-09-10T14:00:00Z",
        ends_at: "2026-09-10T16:00:00Z",
        kind: "reservation" as const,
        status: "confirmed" as const,
      },
    ];

    // Overlapping slot [14:30, 15:00)
    const check1 = checkSlotOccupancy(
      new Date("2026-09-10T14:30:00Z"),
      new Date("2026-09-10T15:00:00Z"),
      occupied
    );
    expect(check1.isOccupied).toBe(true);

    // Adjacent slot [16:00, 16:30) - half open [14:00, 16:00) does NOT overlap
    const check2 = checkSlotOccupancy(
      new Date("2026-09-10T16:00:00Z"),
      new Date("2026-09-10T16:30:00Z"),
      occupied
    );
    expect(check2.isOccupied).toBe(false);
  });
});

describe("Resource Catalog & Availability UI Components (Spec 7.1, 7.2)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderWithClient(ui: React.ReactElement) {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>{ui}</MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("renders ResourceList and performs client-side search within fetched page", async () => {
    const mockResources = {
      items: [
        {
          id: "r-1",
          name: "Community Woodshop",
          location: "Workshop Bay B",
          description: "Equipped with power saws and joinery benches.",
          active: true,
          version: 1,
          created_at: "2026-09-01T00:00:00Z",
          updated_at: "2026-09-01T00:00:00Z",
        },
        {
          id: "r-2",
          name: "Quiet Meeting Room",
          location: "Building 2, Room 204",
          description: "Comfortable room for private meetings.",
          active: true,
          version: 1,
          created_at: "2026-09-01T00:00:00Z",
          updated_at: "2026-09-01T00:00:00Z",
        },
      ],
      total: 2,
      limit: 12,
      offset: 0,
    };

    vi.mocked(fetch).mockResolvedValueOnce({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => mockResources,
    } as Response);

    renderWithClient(<ResourceList />);

    expect(await screen.findByText("Community Woodshop")).toBeInTheDocument();
    expect(screen.getByText("Quiet Meeting Room")).toBeInTheDocument();

    const searchInput = screen.getByLabelText(/search page resources/i);
    fireEvent.change(searchInput, { target: { value: "Woodshop" } });

    expect(screen.getByText("Community Woodshop")).toBeInTheDocument();
    expect(screen.queryByText("Quiet Meeting Room")).not.toBeInTheDocument();
  });

  it("renders ResourceDetail with locked timezone and 7-day occupancy grid", async () => {
    const mockResource = {
      id: "r-1",
      name: "Community Woodshop",
      location: "Workshop Bay B",
      description: "Equipped with power saws.",
      active: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    const mockAvailability = {
      resource_id: "r-1",
      starts_at: "2026-09-10T04:00:00Z",
      ends_at: "2026-09-17T04:00:00Z",
      timezone: "America/New_York",
      occupied: [
        {
          starts_at: "2026-09-10T14:00:00Z",
          ends_at: "2026-09-10T16:00:00Z",
          kind: "reservation" as const,
          status: "confirmed" as const,
        },
      ],
    };

    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/availability")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => mockAvailability,
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"v1"' }),
        json: async () => mockResource,
      } as Response;
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/resources/r-1"]}>
          <Routes>
            <Route path="/resources/:id" element={<ResourceDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("Community Woodshop")).toBeInTheDocument();
    expect(screen.getByText(/Organization Timezone:/i)).toBeInTheDocument();
    expect(screen.getAllByText(/America\/New_York/i).length).toBeGreaterThan(0);

    expect(screen.getByText(/7-Day Availability Schedule/i)).toBeInTheDocument();

    const bookBtn = screen.getByRole("button", {
      name: /booking unavailable/i,
    });
    expect(bookBtn).toBeDisabled();
    expect(bookBtn).toHaveTextContent(/feature registration pending/i);
  });

  it("renders accessible list view alternative without owner identities", async () => {
    const mockResource = {
      id: "r-1",
      name: "Community Woodshop",
      location: "Workshop Bay B",
      description: "Equipped with power saws.",
      active: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    const mockAvailability = {
      resource_id: "r-1",
      starts_at: "2026-09-10T04:00:00Z",
      ends_at: "2026-09-17T04:00:00Z",
      timezone: "America/New_York",
      occupied: [
        {
          starts_at: "2026-09-10T14:00:00Z",
          ends_at: "2026-09-10T16:00:00Z",
          kind: "reservation" as const,
          status: "confirmed" as const,
        },
      ],
    };

    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/availability")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => mockAvailability,
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"v1"' }),
        json: async () => mockResource,
      } as Response;
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/resources/r-1"]}>
          <Routes>
            <Route path="/resources/:id" element={<ResourceDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await screen.findByText("Community Woodshop");

    const listToggle = screen.getByRole("button", { name: /accessible list view/i });
    fireEvent.click(listToggle);

    expect(
      screen.getByLabelText(/accessible 7-day availability schedule/i)
    ).toBeInTheDocument();
    expect(screen.getByText(/Reserved \(confirmed\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/owned by/i)).not.toBeInTheDocument();
  });

  it("displays accessible inline error when invalid past slot is selected (R3)", async () => {
    const mockResource = {
      id: "r-1",
      name: "Community Woodshop",
      location: "Workshop Bay B",
      description: "Equipped with power saws.",
      active: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/availability")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => ({
            resource_id: "r-1",
            starts_at: "2026-09-10T04:00:00Z",
            ends_at: "2026-09-17T04:00:00Z",
            timezone: "America/New_York",
            occupied: [],
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"v1"' }),
        json: async () => mockResource,
      } as Response;
    });

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/resources/r-1"]}>
          <Routes>
            <Route path="/resources/:id" element={<ResourceDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await screen.findByText("Community Woodshop");
    await screen.findByText(/Available & Occupied Slots/i);

    // Click first slot on today (which is in the past)
    const selectSlotBtn = screen.getAllByRole("button", { name: "Select Slot" })[0];
    fireEvent.click(selectSlotBtn);

    // Assert accessible inline error alert is displayed (R3)
    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert).toHaveAttribute("aria-live", "polite");
      expect(alert).toHaveTextContent(/at least 15 minutes in the future/i);
    });
  });

  it("passes typed launch contract without issuing any mutation (R4)", async () => {
    const mockResource = {
      id: "r-1",
      name: "Community Woodshop",
      location: "Workshop Bay B",
      description: "Equipped with power saws.",
      active: true,
      version: 1,
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-01T00:00:00Z",
    };

    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/availability")) {
        return {
          ok: true,
          status: 200,
          headers: new Headers(),
          json: async () => ({
            resource_id: "r-1",
            starts_at: "2026-09-10T04:00:00Z",
            ends_at: "2026-09-17T04:00:00Z",
            timezone: "America/New_York",
            occupied: [],
          }),
        } as Response;
      }
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"v1"' }),
        json: async () => mockResource,
      } as Response;
    });

    const launchSpy = vi.fn();
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/resources/r-1"]}>
          <Routes>
            <Route path="/resources/:id" element={<ResourceDetail onLaunchBooking={launchSpy} />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await screen.findByText("Community Woodshop");
    await screen.findByText(/Available & Occupied Slots/i);

    // Select tomorrow (day index 1)
    const dayButtons = screen.getAllByRole("button").filter((b) => b.textContent?.includes(","));
    if (dayButtons.length > 1) {
      fireEvent.click(dayButtons[1]);
    }

    // Select an available future slot
    await waitFor(() => {
      const btns = screen.getAllByRole("button", { name: "Select Slot" });
      expect(btns.length).toBeGreaterThan(0);
      fireEvent.click(btns[0]);
    });

    // Verify typed contract passed to callback seam
    expect(launchSpy).toHaveBeenCalledTimes(1);
    const contract = launchSpy.mock.calls[0][0] as BookingLaunchContract;
    expect(contract.resource.id).toBe("r-1");
    expect(contract.window.starts_at).toBeDefined();
    expect(contract.window.ends_at).toBeDefined();

    // Verify NO mutation occurred (no POST /api/v1/bookings)
    const calls = vi.mocked(fetch).mock.calls;
    const bookingMutationCalls = calls.filter(([url, opts]) =>
      String(url).includes("/bookings") && opts?.method === "POST"
    );
    expect(bookingMutationCalls).toHaveLength(0);
  });
});

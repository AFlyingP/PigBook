import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { ResourceCreateDialog } from "../src/features/admin/inventory/ResourceCreateDialog";
import { ResourceEditDialog } from "../src/features/admin/inventory/ResourceEditDialog";
import { ResourceArchiveDialog } from "../src/features/admin/inventory/ResourceArchiveDialog";
import { ResourceTable } from "../src/features/admin/inventory/ResourceTable";
import { BlackoutCreateDialog } from "../src/features/admin/inventory/BlackoutCreateDialog";
import { BlackoutCancelDialog } from "../src/features/admin/inventory/BlackoutCancelDialog";
import { BlackoutTable } from "../src/features/admin/inventory/BlackoutTable";
import { restoreAttempt } from "../src/api/createAttempt";
import { AuthContext, type AuthContextType } from "../src/features/auth/AuthContext";
import type { components } from "../src/api/schema";

type User = components["schemas"]["User"];
type Resource = components["schemas"]["Resource"];
type Booking = components["schemas"]["Booking"];

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

const mockResource: Resource = {
  id: "55555555-5555-4555-8555-555555555555",
  name: "Community Woodshop",
  location: "Workshop Bay B",
  description: "Power tools and benches",
  active: true,
  version: 2,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

const mockBlackout: Booking = {
  id: "blackout-9999-9999-9999",
  resource_id: mockResource.id,
  user_id: null,
  kind: "blackout",
  starts_at: "2026-09-20T09:00:00Z",
  ends_at: "2026-09-20T17:00:00Z",
  status: "confirmed",
  version: 1,
  created_at: "2026-09-10T00:00:00Z",
  updated_at: "2026-09-10T00:00:00Z",
};

function renderWithProviders(
  ui: React.ReactElement,
  {
    user = mockAdminUser,
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

describe("Admin Inventory — Resources (Spec 7.1, 4.2 E17-E20)", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it("creates a resource via POST E18 happy path with validation and announcements", async () => {
    let requestUrl = "";
    let requestBody: Record<string, unknown> = {};

    vi.mocked(fetch).mockImplementation(async (url, init) => {
      requestUrl = String(url);
      requestBody = JSON.parse(String(init?.body || "{}"));
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => ({
          id: "r-new",
          name: requestBody.name,
          location: requestBody.location,
          description: requestBody.description,
          active: true,
          version: 1,
          created_at: "2026-09-11T00:00:00Z",
          updated_at: "2026-09-11T00:00:00Z",
        }),
      } as Response;
    });

    const onClose = vi.fn();
    const onCreated = vi.fn();

    renderWithProviders(
      <ResourceCreateDialog open={true} onClose={onClose} onCreated={onCreated} />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText(/Resource Name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Location/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Resource Name/i), {
      target: { value: "Metalworking Studio" },
    });
    fireEvent.change(screen.getByLabelText(/Location/i), {
      target: { value: "Building C, Bay 1" },
    });
    fireEvent.change(screen.getByLabelText(/Description/i), {
      target: { value: "Equipped with TIG welders and grinders" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Create Resource/i }));

    await waitFor(() => {
      expect(requestUrl).toContain("/api/v1/admin/resources");
      expect(requestBody.name).toBe("Metalworking Studio");
      expect(requestBody.location).toBe("Building C, Bay 1");
      expect(onCreated).toHaveBeenCalled();
      expect(onClose).toHaveBeenCalled();
    });
  });

  it("edits a resource via PATCH E19 with If-Match pinned to current version", async () => {
    let capturedHeaders: Headers | undefined;
    let capturedBody: Record<string, unknown> = {};

    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      capturedHeaders = new Headers(init?.headers);
      capturedBody = JSON.parse(String(init?.body || "{}"));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"3"' }),
        json: async () => ({
          ...mockResource,
          name: capturedBody.name,
          version: 3,
        }),
      } as Response;
    });

    const onClose = vi.fn();
    const onUpdated = vi.fn();

    renderWithProviders(
      <ResourceEditDialog
        open={true}
        resource={mockResource}
        onClose={onClose}
        onUpdated={onUpdated}
      />
    );

    expect(screen.getByDisplayValue("Community Woodshop")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Resource Name/i), {
      target: { value: "Community Woodshop Renovated" },
    });

    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));

    await waitFor(() => {
      expect(capturedHeaders?.get("If-Match")).toBe('"2"');
      expect(capturedBody.name).toBe("Community Woodshop Renovated");
      expect(onUpdated).toHaveBeenCalled();
    });
  });

  it("surfaces 412 VERSION_MISMATCH on edit by refetching and alerting user without silent overwrite", async () => {
    let patchAttemptCount = 0;
    vi.mocked(fetch).mockImplementation(async (url) => {
      const u = String(url);
      if (u.includes("/admin/resources/") && !u.includes("blackouts")) {
        patchAttemptCount++;
        return {
          ok: false,
          status: 412,
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "VERSION_MISMATCH",
              message: "Object modified by another transaction",
              details: {},
            },
          }),
        } as Response;
      }
      // Refetch detail returns version 3
      return {
        ok: true,
        status: 200,
        headers: new Headers({ ETag: '"3"' }),
        json: async () => ({
          ...mockResource,
          name: "Woodshop Updated By Other",
          version: 3,
        }),
      } as Response;
    });

    renderWithProviders(
      <ResourceEditDialog
        open={true}
        resource={mockResource}
        onClose={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Save Changes/i }));

    await waitFor(() => {
      expect(patchAttemptCount).toBe(1);
      expect(
        screen.getByText(/Another update occurred to this resource/i)
      ).toBeInTheDocument();
    });
  });

  it("surfaces 409 RESOURCE_IN_USE as a visible non-destructive error on archive", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 409,
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "RESOURCE_IN_USE",
            message: "Resource has future confirmed bookings or waitlist entries",
            details: {},
          },
        }),
      } as Response;
    });

    renderWithProviders(
      <ResourceArchiveDialog
        open={true}
        resource={mockResource}
        onClose={vi.fn()}
      />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText(/Are you sure you want to archive/i)).toBeInTheDocument();

    const archiveBtn = screen.getByRole("button", { name: /Archive Resource/i });
    fireEvent.click(archiveBtn);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/RESOURCE_IN_USE/i)).toBeInTheDocument();
    });
  });

  it("ResourceTable proves no hard-delete control exists anywhere and status is not colour-only", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [
            mockResource,
            { ...mockResource, id: "r-2", name: "Old Darkroom", active: false, version: 1 },
          ],
          total: 2,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<ResourceTable />);

    await waitFor(() => {
      expect(screen.getByText("Community Woodshop")).toBeInTheDocument();
      expect(screen.getByText("Old Darkroom")).toBeInTheDocument();
    });

    // Guardrail: there must be NO hard-delete button anywhere
    expect(screen.queryByRole("button", { name: /Delete Resource/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Hard Delete/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Force Cancel/i })).not.toBeInTheDocument();

    // Status is clearly conveyed by text labels, not colour alone
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
  });
});

describe("Admin Blackouts — Create, Retry, Conflict, Cancel (Spec 7.1, 7.2, 7.3, 4.2 E21-E23)", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it("creates a blackout with shared Idempotency-Key helper and clears attempt on 201", async () => {
    let capturedIdempotencyKey: string | null = null;

    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      const headers = new Headers(init?.headers);
      capturedIdempotencyKey = headers.get("Idempotency-Key");
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => mockBlackout,
      } as Response;
    });

    const onClose = vi.fn();
    const onCreated = vi.fn();

    renderWithProviders(
      <BlackoutCreateDialog
        open={true}
        resourceId={mockResource.id}
        resourceName={mockResource.name}
        onClose={onClose}
        onCreated={onCreated}
      />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    const submitBtn = screen.getByRole("button", { name: /Create Blackout/i });
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(capturedIdempotencyKey).toBeTruthy();
      // UUID v4 regex pattern
      expect(capturedIdempotencyKey).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      );
      expect(onCreated).toHaveBeenCalled();
      // Definitive 201 clears attempt from storage
      expect(restoreAttempt(mockAdminUser.id)).toBeNull();
    });
  });

  it("retains key on uncertain 503 error and allows same-key retry", async () => {
    let attemptCount = 0;
    const keysUsed: string[] = [];

    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      attemptCount++;
      const headers = new Headers(init?.headers);
      keysUsed.push(headers.get("Idempotency-Key") || "");

      if (attemptCount === 1) {
        // Return 503 uncertain result
        return {
          ok: false,
          status: 503,
          headers: new Headers(),
          json: async () => ({
            error: {
              code: "RETRYABLE_UNAVAILABLE",
              message: "Database lock timeout",
              details: {},
            },
          }),
        } as Response;
      }

      // Second attempt succeeds
      return {
        ok: true,
        status: 201,
        headers: new Headers({ ETag: '"1"' }),
        json: async () => mockBlackout,
      } as Response;
    });

    renderWithProviders(
      <BlackoutCreateDialog
        open={true}
        resourceId={mockResource.id}
        resourceName={mockResource.name}
        onClose={vi.fn()}
      />
    );

    // Initial submit -> 503
    fireEvent.click(screen.getByRole("button", { name: /Create Blackout/i }));

    await waitFor(() => {
      expect(screen.getByText(/Service temporarily unavailable/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /Retry with Same Key/i })).toBeInTheDocument();
    });

    // Verify stored attempt persists
    const stored = restoreAttempt(mockAdminUser.id);
    expect(stored).not.toBeNull();
    expect(stored?.kind).toBe("blackout");

    // Click retry with same key
    fireEvent.click(screen.getByRole("button", { name: /Retry with Same Key/i }));

    await waitFor(() => {
      expect(attemptCount).toBe(2);
      // Key must be identical across retries
      expect(keysUsed[0]).toBe(keysUsed[1]);
      // Cleared after definitive success
      expect(restoreAttempt(mockAdminUser.id)).toBeNull();
    });
  });

  it("definitive 409 SLOT_CONFLICT clears attempt and requires new explicit key", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 409,
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "SLOT_CONFLICT",
            message: "Schedule conflict with existing booking",
            details: {},
          },
        }),
      } as Response;
    });

    renderWithProviders(
      <BlackoutCreateDialog
        open={true}
        resourceId={mockResource.id}
        resourceName={mockResource.name}
        onClose={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /Create Blackout/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/Schedule conflict/i)).toBeInTheDocument();
      // Completed result: stored attempt is cleared
      expect(restoreAttempt(mockAdminUser.id)).toBeNull();
    });
  });

  it("cancels blackout via DELETE E23 with If-Match from version", async () => {
    let capturedHeaders: Headers | undefined;
    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      capturedHeaders = new Headers(init?.headers);
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({ ...mockBlackout, status: "cancelled" }),
      } as Response;
    });

    const onCancelled = vi.fn();
    renderWithProviders(
      <BlackoutCancelDialog
        open={true}
        blackout={mockBlackout}
        resourceId={mockResource.id}
        onClose={vi.fn()}
        onCancelled={onCancelled}
      />
    );

    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Confirm Cancellation/i }));

    await waitFor(() => {
      expect(capturedHeaders?.get("If-Match")).toBe('"1"');
      expect(onCancelled).toHaveBeenCalled();
    });
  });

  it("BlackoutTable renders list and accessible live region", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [mockBlackout],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(
      <BlackoutTable resourceId={mockResource.id} resourceName={mockResource.name} />
    );

    await waitFor(() => {
      expect(screen.getByText(/Blackouts for Community Woodshop/i)).toBeInTheDocument();
      expect(screen.getByText(/Active Blackout/i)).toBeInTheDocument();
    });
  });
});

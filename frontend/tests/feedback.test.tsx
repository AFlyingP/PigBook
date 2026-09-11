import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { FeedbackForm, CONSENT_VERSION } from "../src/features/feedback/FeedbackForm";
import { AdminFeedbackTable } from "../src/features/feedback/AdminFeedbackTable";
import { AuthContext, type AuthContextType } from "../src/features/auth/AuthContext";
import type { components } from "../src/api/schema";

type User = components["schemas"]["User"];
type Feedback = components["schemas"]["Feedback"];

const mockMemberUser: User = {
  id: "user-2222-2222-2222",
  email: "member@example.com",
  display_name: "Member User",
  role: "member",
  enabled: true,
  version: 1,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

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

const mockFeedbackItem: Feedback = {
  id: "fb-1111-1111-1111",
  user_id: "user-2222-2222-2222",
  rating: 4,
  task_completed: true,
  difficulty: "Initial time slot navigation was slightly tricky on mobile.",
  improvement: "Add a 15-minute quick reservation shortcut.",
  consent_version: "2026-09-v1",
  created_at: "2026-09-12T10:00:00Z",
};

function renderWithProviders(
  ui: React.ReactElement,
  {
    user = mockMemberUser,
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

describe("Feedback UI (Spec 7.2, 12.2, 4.2 E33-E34)", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders consent checkbox as UNCHECKED on first render (Spec 12.2)", () => {
    renderWithProviders(<FeedbackForm />);

    const consentCheckbox = screen.getByLabelText(
      /I consent to having my survey responses recorded/i
    );
    expect(consentCheckbox).toBeInTheDocument();
    expect(consentCheckbox).not.toBeChecked();

    // Verify privacy link is present and points to /privacy
    const privacyLink = screen.getByRole("link", { name: /Privacy Notice/i });
    expect(privacyLink).toBeInTheDocument();
    expect(privacyLink).toHaveAttribute("href", "/privacy");
  });

  it("requires affirmative consent on submission and displays inline error without losing entered text", async () => {
    renderWithProviders(<FeedbackForm />);

    // Fill in difficulty and improvement
    const difficultyInput = screen.getByLabelText(/What, if anything, was difficult/i);
    const improvementInput = screen.getByLabelText(/What is one improvement/i);

    fireEvent.change(difficultyInput, { target: { value: "Finding parking information was unclear." } });
    fireEvent.change(improvementInput, { target: { value: "Link map directly to details page." } });

    // Submit without checking consent
    const submitBtn = screen.getByRole("button", { name: /Submit Feedback/i });
    fireEvent.click(submitBtn);

    // Verify inline error is announced and text is preserved
    await waitFor(() => {
      expect(screen.getByText(/Consent is required to submit survey responses/i)).toBeInTheDocument();
    });

    expect(screen.getByDisplayValue("Finding parking information was unclear.")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Link map directly to details page.")).toBeInTheDocument();
  });

  it("handles backend 422 CONSENT_REQUIRED inline without losing text", async () => {
    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: false,
        status: 422,
        headers: new Headers(),
        json: async () => ({
          error: {
            code: "CONSENT_REQUIRED",
            message: "Affirmative consent required",
            details: {},
          },
        }),
      } as Response;
    });

    renderWithProviders(<FeedbackForm />);

    fireEvent.change(screen.getByLabelText(/What, if anything, was difficult/i), {
      target: { value: "Text that must not be lost." },
    });

    // Check consent locally so form attempts submission
    const consentCheckbox = screen.getByLabelText(/I consent to having my survey responses recorded/i);
    fireEvent.click(consentCheckbox);

    fireEvent.click(screen.getByRole("button", { name: /Submit Feedback/i }));

    await waitFor(() => {
      expect(screen.getByText(/Affirmative consent is required/i)).toBeInTheDocument();
      expect(screen.getByDisplayValue("Text that must not be lost.")).toBeInTheDocument();
    });
  });

  it("submits feedback successfully with accurate payload shape and consent_version", async () => {
    let capturedBody: Record<string, unknown> = {};

    vi.mocked(fetch).mockImplementation(async (_url, init) => {
      capturedBody = JSON.parse(String(init?.body || "{}"));
      return {
        ok: true,
        status: 201,
        headers: new Headers(),
        json: async () => ({ id: "fb-created-1", created_at: "2026-09-12T12:00:00Z" }),
      } as Response;
    });

    renderWithProviders(<FeedbackForm />);

    // Answer questions
    fireEvent.change(screen.getByLabelText(/What, if anything, was difficult/i), {
      target: { value: "None, smooth booking." },
    });
    fireEvent.change(screen.getByLabelText(/What is one improvement/i), {
      target: { value: "Add calendar export." },
    });

    // Check consent
    fireEvent.click(screen.getByLabelText(/I consent to having my survey responses recorded/i));

    fireEvent.click(screen.getByRole("button", { name: /Submit Feedback/i }));

    await waitFor(() => {
      expect(screen.getByText(/Feedback Submitted Successfully/i)).toBeInTheDocument();
      expect(capturedBody.rating).toBe(5);
      expect(capturedBody.task_completed).toBe(true);
      expect(capturedBody.difficulty).toBe("None, smooth booking.");
      expect(capturedBody.improvement).toBe("Add calendar export.");
      expect(capturedBody.consent_version).toBe(CONSENT_VERSION);
      expect(capturedBody.consent).toBe(true);
    });
  });

  it("AdminFeedbackTable renders only authorized Spec 4.1 fields and renders free text as plain text", async () => {
    const maliciousText = '<script>alert("XSS")</script><img src="x" onerror="alert(1)">';
    const feedbackWithXss: Feedback = {
      ...mockFeedbackItem,
      difficulty: maliciousText,
    };

    vi.mocked(fetch).mockImplementation(async () => {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          items: [feedbackWithXss],
          total: 1,
          limit: 25,
          offset: 0,
        }),
      } as Response;
    });

    renderWithProviders(<AdminFeedbackTable />, { user: mockAdminUser });

    await waitFor(() => {
      expect(screen.getByText(/Participant Feedback Responses/i)).toBeInTheDocument();
      // user_id is shown
      expect(screen.getByText("user-2222-2222-2222")).toBeInTheDocument();
      // rating is shown
      expect(screen.getByText("(4/5)")).toBeInTheDocument();
      // task completed is shown
      expect(screen.getByText("Yes")).toBeInTheDocument();
      // consent version is shown
      expect(screen.getByText("2026-09-v1")).toBeInTheDocument();
    });

    // Spec guardrail: NEVER display identities beyond user_id
    expect(screen.queryByText("member@example.com")).not.toBeInTheDocument();
    expect(screen.queryByText("Member User")).not.toBeInTheDocument();

    // Plain text rendering of free text: script tags must be rendered as visible text, NOT DOM elements
    expect(document.querySelector("script[src]")).toBeNull();
    expect(document.querySelector("img[onerror]")).toBeNull();
    expect(screen.getByText(maliciousText)).toBeInTheDocument();
  });
});

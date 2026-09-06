import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import App from "../src/App";

describe("App", () => {
  beforeEach(() => {
    vi.spyOn(globalThis, "fetch");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a heading with accessible name CommonsBook", () => {
    render(<App />);
    const heading = screen.getByRole("heading", { name: "CommonsBook" });
    expect(heading).toBeInTheDocument();
  });

  it("renders the explanatory paragraph text", () => {
    render(<App />);
    const paragraph = screen.getByText(
      /equipment and room reservations for a single community group; accounts are invitation-only/i,
    );
    expect(paragraph).toBeInTheDocument();
  });

  it("makes no network request", () => {
    render(<App />);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

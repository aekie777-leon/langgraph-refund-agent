import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("OpsPilot showcase", () => {
  it("explains that the backend remains the trust boundary", () => {
    render(<App />);
    expect(screen.getByText(/backend still derives tenant/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run through Graph" })).toBeEnabled();
  });

  it("does not persist a selected persona in browser storage", () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    render(<App />);
    fireEvent.change(screen.getByLabelText("Acting persona"), { target: { value: "sup-1" } });
    expect(screen.getByText("supervisor")).toBeInTheDocument();
    expect(storage).not.toHaveBeenCalled();
  });

  it("shows a safe authorization failure from the backend", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      json: async () => ({ error: { message: "Permission denied." } })
    }));
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Case queue" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh queue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("403 · Permission denied.");
  });

  it("renders payload-free retry evidence for the supervisor", async () => {
    const fetchMock = vi.fn().mockImplementation((input: string) => {
      const isActivity = input.includes("/attempts");
      return Promise.resolve({
        ok: true,
        json: async () => isActivity
          ? {
              items: [{
                queue: "outbox",
                resource_id: "resource-1",
                command_id: "command-123456789",
                cycle: 1,
                attempt_number: 1,
                outcome: "retry_scheduled",
                failure_kind: "http_retryable",
                http_status: 500,
                safe_error_code: "provider_http_500",
                started_at: "2026-08-20T10:00:00Z",
                finished_at: "2026-08-20T10:00:01Z"
              }],
              generated_at: "2026-08-20T10:00:02Z"
            }
          : { outbox: [], inbox: [], generated_at: "2026-08-20T10:00:02Z" }
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.change(screen.getByLabelText("Acting persona"), {
      target: { value: "sup-1" }
    });
    fireEvent.click(screen.getByRole("button", { name: "Provider Ops" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh queues" }));

    expect(await screen.findByText("retry_scheduled")).toBeInTheDocument();
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
    expect(screen.getByText(/Payloads, customer data/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

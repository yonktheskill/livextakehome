import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";

vi.mock("../components/auth", () => ({
  useAuth: () => ({ logout: vi.fn(), refresh: authorized }),
}));
const authorized = () => {};
const user = { id: 1, email: "owner@example.test", time_zone: "UTC" };
const json = (value: unknown) => new Response(JSON.stringify(value));

describe("chat composer controls", () => {
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("replaces Send with Stop during a run and restores Send with the draft intact", async () => {
    let events!: ReadableStreamDefaultController<Uint8Array>;
    let confirmStop!: (response: Response) => void;
    const stopResponse = new Promise<Response>((resolve) => {
      confirmStop = resolve;
    });
    const fetcher = vi.fn(async (url: string) => {
      if (url.endsWith("/history")) return json({ messages: [], next_cursor: null });
      if (url.endsWith("/connection")) return json({ connected: true, label: "Connected" });
      if (url === "/api/chat/messages") return json({ run_id: "run-1", message_id: "u", revision: 1 });
      if (url.endsWith("/stop")) return stopResponse;
      if (url.includes("/events"))
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              events = controller;
            },
          }),
        );
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    const { unmount } = render(<Chat user={user} />);
    await screen.findByRole("heading", { name: "What’s on your calendar?" });
    const input = screen.getByRole("textbox", { name: "Message HelpBook" });
    fireEvent.change(input, { target: { value: "Book a meeting" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));

    const stop = await screen.findByRole("button", { name: "Stop response" });
    expect(screen.queryByRole("button", { name: "Send message" })).not.toBeInTheDocument();
    expect(stop.closest("form")).toBe(input.closest("form"));
    expect(stop).toBeEnabled();
    fireEvent.change(input, { target: { value: "Keep this draft" } });
    fireEvent.click(stop);
    expect(await screen.findByRole("button", { name: "Stopping response" })).toBeDisabled();
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/chat/messages")).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledWith("/api/runs/run-1/stop", expect.objectContaining({ method: "POST" }));

    await act(async () => {
      confirmStop(json({ status: "running", dispatch_committed: false }));
    });
    // Stop stays in place until the stream confirms the run has ended.
    expect(screen.getByRole("button", { name: "Stop response" })).toBeInTheDocument();
    await act(async () => {
      events.enqueue(new TextEncoder().encode("id: 1\nevent: run.interrupted\ndata: {}\n\n"));
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled());
    expect(screen.queryByRole("button", { name: "Stop response" })).not.toBeInTheDocument();
    expect(input).toHaveValue("Keep this draft");
    unmount();
  });
});

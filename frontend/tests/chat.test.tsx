import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function renderMessages() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/history"))
          return json({
            messages: [
              { id: "u", role: "user", content: "My **request**\nwith a second line" },
              { id: "a", role: "assistant", content: "Agent **reply**\n\n- First item\n- Second item" },
            ].map((message) => ({
              ...message,
              created_at: "2026-01-01T12:00:00Z",
              status: "completed",
            })),
            next_cursor: null,
          });
        if (url.endsWith("/connection")) return json({ connected: true, label: "Connected" });
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    render(<Chat user={user} />);
    return screen.findAllByRole("button", { name: "Copy message" });
  }

  it("copies each user and agent message verbatim with independent feedback", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const buttons = await renderMessages();

    fireEvent.click(buttons[0]);
    await waitFor(() => expect(buttons[0]).toHaveTextContent("Copied"));
    expect(writeText).toHaveBeenLastCalledWith("My **request**\nwith a second line");
    expect(buttons[1]).toHaveTextContent(/^Copy$/);

    fireEvent.click(buttons[1]);
    await waitFor(() => expect(buttons[1]).toHaveTextContent("Copied"));
    expect(writeText).toHaveBeenLastCalledWith("Agent **reply**\n\n- First item\n- Second item");
  });

  it.each(["rejected", "unavailable"])("handles a %s clipboard without claiming success", async (mode) => {
    const writeText = vi.fn().mockRejectedValue(new Error("Clipboard permission denied"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: mode === "unavailable" ? undefined : { writeText },
    });
    const [button] = await renderMessages();
    fireEvent.click(button);
    const message = within(button.closest("article")!);
    expect(await message.findByText("Unable to copy. Please try again.")).toBeInTheDocument();
    expect(button).toHaveTextContent(/^Copy$/);
    expect(button).toBeEnabled();
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

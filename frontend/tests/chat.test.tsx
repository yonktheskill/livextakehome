import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat, MAX_MESSAGE_CHARACTERS } from "../components/chat";

vi.mock("../components/auth", () => ({
  useAuth: () => ({ logout: vi.fn(), refresh: authorized }),
}));
const authorized = () => {};
const user = { id: 1, email: "owner@example.test", time_zone: "UTC" };
const json = (value: unknown) => new Response(JSON.stringify(value));

describe("chat composer controls", () => {
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
    else Reflect.deleteProperty(navigator, "clipboard");
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
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

  it("keeps oversized drafts intact, blocks Enter, and accepts the exact character limit", async () => {
    await renderMessages();
    const input = screen.getByRole("textbox", { name: "Message HelpBook" });
    const send = screen.getByRole("button", { name: "Send message" });
    const tail = " Attendee: Tail tail@example.test";
    const valid = "x".repeat(MAX_MESSAGE_CHARACTERS - tail.length) + tail;
    expect(MAX_MESSAGE_CHARACTERS).toBe(6000);
    fireEvent.change(input, { target: { value: valid + "x" } });
    expect(input).toHaveValue(valid + "x");
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent("Shorten it to 6,000 characters");
    expect(send).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === "/api/chat/messages")).toBe(false);
    expect(input).toHaveValue(valid + "x");
    fireEvent.change(input, { target: { value: valid } });
    expect(input).toHaveValue(valid);
    expect(send).toBeEnabled();
    expect(screen.getByText("6,000 / 6,000 characters")).toBeInTheDocument();
    // Match Python/Pydantic's Unicode character count, including astral characters.
    fireEvent.change(input, { target: { value: "🙂".repeat(MAX_MESSAGE_CHARACTERS) } });
    expect(send).toBeEnabled();
    expect(screen.getByText("6,000 / 6,000 characters")).toBeInTheDocument();
  });

  it("passes the account timezone to receipts restored from history", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/history"))
          return json({
            messages: [],
            next_cursor: null,
            receipts: [
              {
                action_id: "booking-action",
                kind: "create_booking",
                summary: "Cal.com returned an action result; inspect its status.",
                details: {
                  status: "accepted",
                  start: "2026-11-06T04:00:00Z",
                  end: "2026-11-06T04:40:00Z",
                  booking_uid: "confirmed-booking",
                },
              },
            ],
          });
        if (url.endsWith("/connection")) return json({ connected: true, label: "Connected" });
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    render(<Chat user={{ ...user, time_zone: "Asia/Singapore" }} />);
    expect(await screen.findByText("Friday, 06-11-2026, 12:00 Asia/Singapore")).toBeInTheDocument();
    expect(screen.getByText("Friday, 06-11-2026, 12:40 Asia/Singapore")).toBeInTheDocument();
    expect(screen.getByText("confirmed-booking")).toBeInTheDocument();
    expect(screen.queryByText(/Action receipt \(/)).not.toBeInTheDocument();
  });

  it("shows confirmations once in chat and reserves result cards for booking creation", async () => {
    const confirmation =
      "Shall I create “Intro” as a reusable meeting type?\n\n- **Duration:** 30 minutes\n- **Location:** Cal Video\n\nReply **yes** to confirm, or tell me what you’d like to change.";
    const result = "Created “Intro” as a reusable 30-minute meeting type.";
    const messages = [result, confirmation].map((content, index) => ({
      id: `a${index}`,
      role: "assistant",
      content,
      status: "final",
      created_at: "2026-11-05T00:00:00Z",
    }));
    const receipts = ["create_event_type", "reschedule_booking", "cancel_booking", "create_booking"].map(
      (kind) => ({
        action_id: kind,
        kind,
        summary: `Result for ${kind}`,
        details: { booking_uid: "confirmed-booking" },
      }),
    );
    let historyReads = 0;
    let events!: ReadableStreamDefaultController<Uint8Array>;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/history"))
          return json(
            ++historyReads === 1
              ? { messages: [], receipts: [], next_cursor: null }
              : { messages, receipts, next_cursor: null },
          );
        if (url.endsWith("/connection")) return json({ connected: true });
        if (url === "/api/chat/messages") return json({ run_id: "run-1", message_id: "u", revision: 1 });
        if (url.includes("/events"))
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                events = controller;
              },
            }),
          );
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    render(<Chat user={user} />);
    await screen.findByRole("heading", { name: "What’s on your calendar?" });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Book a meeting" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Stop response" });
    await act(async () => {
      const frames = [
        ...receipts.map((data) => ({ type: "action.result", data })),
        ...messages.map((message) => ({
          type: "message.delta",
          data: { message_id: message.id, text: message.content },
        })),
      ];
      events.enqueue(
        new TextEncoder().encode(
          frames
            .map(
              ({ type, data }, index) =>
                `id: ${index + 1}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`,
            )
            .join(""),
        ),
      );
    });
    const assertDisplay = () => {
      expect(screen.getAllByText("Shall I create “Intro” as a reusable meeting type?")).toHaveLength(1);
      expect(screen.getByText("30 minutes")).toBeInTheDocument();
      expect(screen.getByText("Cal Video")).toBeInTheDocument();
      expect(screen.getByText(result).closest("article")).toHaveClass("message-assistant");
      expect(document.querySelectorAll(".receipt")).toHaveLength(1);
      expect(screen.getByText("create booking")).toBeInTheDocument();
      for (const kind of ["create_event_type", "reschedule_booking", "cancel_booking"])
        expect(screen.queryByText(`Result for ${kind}`)).not.toBeInTheDocument();
    };
    assertDisplay();
    await act(async () => {
      events.enqueue(
        new TextEncoder().encode(
          `id: 7\nevent: run.waiting\ndata: ${JSON.stringify({ message: confirmation })}\n\n`,
        ),
      );
    });
    await waitFor(() => expect(historyReads).toBe(2));
    expect(screen.queryByRole("button", { name: "Stop response" })).not.toBeInTheDocument();
    assertDisplay();
    expect(document.querySelector(".run-status")).not.toBeInTheDocument();
  });

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

  it("sends a correction by pointer while keeping Stop visible", async () => {
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.includes("history")) return json({ messages: [], next_cursor: null });
        if (url.endsWith("/connection")) return json({ connected: true });
        if (url === "/api/chat/messages") {
          posts.push(JSON.parse(init!.body as string).content);
          return json({ run_id: "r", message_id: `u${posts.length}`, revision: posts.length });
        }
        return new Response(new ReadableStream());
      }),
    );
    const { unmount } = render(<Chat user={user} />);
    await screen.findByRole("heading", { name: "What’s on your calendar?" });
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Book a meeting" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Stop response" });
    fireEvent.change(input, { target: { value: "Use tomorrow instead" } });
    expect(screen.getByRole("button", { name: "Stop response" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(posts).toEqual(["Book a meeting", "Use tomorrow instead"]));
    unmount();
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
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
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

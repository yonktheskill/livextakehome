import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";
import type { Message, Receipt } from "../lib/api";

const refresh = () => {};
vi.mock("../components/auth", () => ({ useAuth: () => ({ refresh, logout: vi.fn() }) }));
const user = { id: 1, email: "owner@example.test", time_zone: "UTC" };
const json = (value: unknown) => new Response(JSON.stringify(value));
const message = (id: string, created_at: string): Message => ({
  id,
  role: "assistant",
  content: id,
  created_at,
  status: "final",
});
const receipt = (id: string, created_at: string): Receipt => ({
  action_id: id,
  message_id: `anchor:${id}`,
  kind: "create_booking",
  created_at,
  summary: `Booked ${id}`,
  // The appointment date must not determine the card's place in the chat.
  details: { booking_uid: id, start: "2026-12-01T12:00:00Z" },
});

function expectOrder(labels: string[]) {
  const articles = Array.from(document.querySelectorAll(".conversation-content > article"));
  expect(articles).toEqual(labels.map((label) => screen.getByText(label).closest("article")));
  for (const article of articles) expect(article.closest(".conversation-scroll")).not.toBeNull();
  expect(document.querySelector(".composer-area .receipt")).toBeNull();
}

describe("booking cards in the conversation", () => {
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
  });

  it("restores multiple cards between their surrounding messages, including within one millisecond", async () => {
    const messages = [
      message("First request", "2026-11-05T00:00:00.000100+00:00"),
      message("First reply", "2026-11-05T00:00:00.000300+00:00"),
      message("Second request", "2026-11-05T01:00:00Z"),
      message("Second reply", "2026-11-05T01:02:00Z"),
      message("Later conversation", "2026-11-05T02:00:00Z"),
    ];
    // Receipt query order need not match creation order.
    const receipts = [
      receipt("second", "2026-11-05T01:01:00Z"),
      receipt("first", "2026-11-05T00:00:00.000200+00:00"),
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("/history")
          ? json({ messages, receipts, next_cursor: null })
          : json({ connected: true }),
      ),
    );
    render(<Chat user={user} />);
    await screen.findByText("Booked first");
    expectOrder([
      "First request",
      "Booked first",
      "First reply",
      "Second request",
      "Booked second",
      "Second reply",
      "Later conversation",
    ]);
  });

  it("keeps a streamed card before later replies through replay and history reload", async () => {
    let events!: ReadableStreamDefaultController<Uint8Array>;
    let historyReads = 0;
    const now = Date.now();
    const booking = receipt("live", new Date(now + 1000).toISOString());
    const reply = message("Your booking is ready", new Date(now + 2000).toISOString());
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/history"))
          return json(
            ++historyReads === 1
              ? { messages: [], receipts: [], next_cursor: null }
              : {
                  messages: [message("Book a meeting", new Date(now).toISOString()), reply],
                  receipts: [booking],
                  next_cursor: null,
                },
          );
        if (url.endsWith("/connection")) return json({ connected: true });
        if (url === "/api/chat/messages") return json({ run_id: "r", message_id: "u", revision: 1 });
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              events = controller;
            },
          }),
        );
      }),
    );
    const view = render(<Chat user={user} />);
    await screen.findByRole("heading", { name: "What’s on your calendar?" });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Book a meeting" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("button", { name: "Stop response" });
    const frame = (id: number, type: string, data: unknown) =>
      `id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`;
    await act(async () => {
      events.enqueue(
        new TextEncoder().encode(
          frame(1, "action.result", booking) +
            frame(1, "action.result", booking) +
            frame(2, "message.delta", {
              message_id: reply.id,
              text: reply.content,
              created_at: reply.created_at,
            }),
        ),
      );
    });
    expectOrder(["Book a meeting", "Booked live", "Your booking is ready"]);
    await act(async () => {
      events.enqueue(new TextEncoder().encode(frame(3, "run.completed", {})));
    });
    await waitFor(() => expect(historyReads).toBe(2));
    expectOrder(["Book a meeting", "Booked live", "Your booking is ready"]);
    view.unmount();
    render(<Chat user={user} />);
    await screen.findByText("Booked live");
    expectOrder(["Book a meeting", "Booked live", "Your booking is ready"]);
  });

  it("places paginated older cards in history without duplicating cards returned on both pages", async () => {
    const old = receipt("old", "2026-11-04T00:01:00Z");
    const recent = receipt("recent", "2026-11-05T00:01:00Z");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("before="))
          return json({
            messages: [message("Older request", "2026-11-04T00:00:00Z")],
            receipts: [old, recent],
            next_cursor: null,
          });
        if (url.endsWith("/history"))
          return json({
            messages: [
              message("Recent request", "2026-11-05T00:00:00Z"),
              message("Latest reply", "2026-11-05T00:02:00Z"),
            ],
            receipts: [recent],
            next_cursor: "older",
          });
        return json({ connected: true });
      }),
    );
    render(<Chat user={user} />);
    fireEvent.click(await screen.findByRole("button", { name: "Load earlier messages" }));
    await screen.findByText("Booked old");
    expectOrder(["Older request", "Booked old", "Recent request", "Booked recent", "Latest reply"]);
  });
});

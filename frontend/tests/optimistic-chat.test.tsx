import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";

const authorized = () => {};
vi.mock("../components/auth", () => ({
  useAuth: () => ({ logout: vi.fn(), refresh: authorized }),
}));
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("optimistic chat delivery", () => {
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    if (originalScroll) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScroll);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function renderPendingSend() {
    let respond!: (response: Response) => void;
    const response = new Promise<Response>((resolve) => {
      respond = resolve;
    });
    let events!: ReadableStreamDefaultController<Uint8Array>;
    let historyReads = 0;
    const fetcher = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async (url) => {
      if (url.endsWith("/history"))
        return json({
          messages:
            ++historyReads > 1
              ? [
                  {
                    id: "u",
                    role: "user",
                    content: "Book a meeting",
                    status: "sent",
                    created_at: "2026-01-01",
                  },
                ]
              : [],
          next_cursor: null,
        });
      if (url.endsWith("/connection")) return json({ connected: true, label: "Connected" });
      if (url === "/api/chat/messages") return response;
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
    return {
      ...render(<Chat user={{ id: 1, email: "owner@example.test", time_zone: "UTC" }} />),
      respond,
      fetcher,
      finish: () => events.enqueue(new TextEncoder().encode("id: 1\nevent: run.completed\ndata: {}\n\n")),
    };
  }

  async function sendMessage() {
    await screen.findByRole("heading", { name: "What’s on your calendar?" });
    const input = screen.getByRole("textbox", { name: "Message HelpBook" });
    fireEvent.change(input, { target: { value: "Book a meeting" } });
    fireEvent.click(screen.getByRole("button", { name: "Send message" }));
    return input;
  }

  it("shows the message and clears the composer before acceptance, then reconciles history", async () => {
    const { respond, fetcher, finish, unmount } = renderPendingSend();
    const input = await sendMessage();
    expect(screen.getByText("Book a meeting")).toBeInTheDocument();
    expect(screen.getByText("Sending…")).toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(screen.queryByRole("heading", { name: "What’s on your calendar?" })).not.toBeInTheDocument();
    fireEvent.change(input, { target: { value: "My next draft" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/chat/messages")).toHaveLength(1);
    await act(async () => {
      respond(json({ run_id: "r", message_id: "u", revision: 1 }));
    });
    expect(screen.getAllByText("Book a meeting")).toHaveLength(1);
    expect(screen.queryByText("Sending…")).not.toBeInTheDocument();
    expect(input).toHaveValue("My next draft");
    await act(async () => {
      finish();
    });
    await waitFor(() =>
      expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/history"))).toHaveLength(2),
    );
    expect(screen.getAllByText("Book a meeting")).toHaveLength(1);
    unmount();
  });

  it.each([false, true])(
    "removes a rejected message and restores the draft without overwriting edits (%s)",
    async (editDraft) => {
      const { respond, unmount } = renderPendingSend();
      const input = await sendMessage();
      if (editDraft) fireEvent.change(input, { target: { value: "A different draft" } });
      await act(async () => {
        respond(json({ detail: "Message rejected" }, 422));
      });
      expect(screen.getByRole("alert")).toHaveTextContent("Message rejected");
      expect(screen.queryAllByRole("article")).toHaveLength(0);
      expect(input).toHaveValue(editDraft ? "A different draft" : "Book a meeting");
      expect(screen.queryByRole("button", { name: "Retry message" })).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
      unmount();
    },
  );

  it("keeps uncertain delivery visible and retries without duplicating it or clearing a new draft", async () => {
    const { respond, fetcher, unmount } = renderPendingSend();
    const input = await sendMessage();
    await act(async () => {
      respond(json({ detail: "Service unavailable" }, 503));
    });
    expect(screen.getByText("Delivery unconfirmed")).toBeInTheDocument();
    expect(input).toHaveValue("Book a meeting");
    fireEvent.change(input, { target: { value: "A different draft" } });
    let retryRespond!: (response: Response) => void;
    fetcher.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          retryRespond = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
    fireEvent.change(input, { target: { value: "New draft while retrying" } });
    await act(async () => {
      retryRespond(json({ run_id: "r", message_id: "u", revision: 1 }));
    });
    await screen.findByRole("button", { name: "Stop response" });
    expect(screen.getAllByText("Book a meeting")).toHaveLength(1);
    expect(screen.queryByText("Delivery unconfirmed")).not.toBeInTheDocument();
    expect(input).toHaveValue("New draft while retrying");
    const posts = fetcher.mock.calls.filter(([url]) => url === "/api/chat/messages");
    expect(posts).toHaveLength(2);
    expect(posts[0][1]?.body).toBe(posts[1][1]?.body);
    unmount();
  });
});

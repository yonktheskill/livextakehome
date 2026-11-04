import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConversation } from "../lib/use-conversation";
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function stream(text: string) {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(text));
        controller.close();
      },
    }),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}
const authorized = () => {};
describe("conversation delivery and recovery", () => {
  beforeEach(() => {
    sessionStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });
  it("deduplicates replayed events and reloads canonical history on completion", async () => {
    let historyReads = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes("/history"))
        return json({
          messages:
            ++historyReads > 1
              ? [
                  {
                    id: "a",
                    role: "assistant",
                    content: "Hello",
                    created_at: "2026-01-01",
                    status: "complete",
                  },
                ]
              : [],
          next_cursor: null,
        });
      if (url === "/api/chat/messages") return json({ run_id: "r", message_id: "u", revision: 1 });
      return stream(
        'id: 1\nevent: message.delta\ndata: {"message_id":"a","text":"Hello"}\n\nid: 1\nevent: message.delta\ndata: {"message_id":"a","text":"Hello"}\n\nid: 2\nevent: run.completed\ndata: {}\n\n',
      );
    });
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.submit("Hello");
    });
    await waitFor(() => expect(result.current.run).toBeNull());
    expect(result.current.messages.find((m) => m.id === "a")?.content).toBe("Hello");
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/chat/messages")).toHaveLength(1);
  });
  it("retries uncertain delivery only with the original client request ID", async () => {
    const submissions: string[] = [];
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("history")) return json({ messages: [], next_cursor: null });
      if (url === "/api/chat/messages") {
        submissions.push(init!.body as string);
        if (submissions.length === 1) throw new TypeError("Network disconnected");
        return json({ run_id: "r", message_id: "u", revision: 1 });
      }
      return new Response(new ReadableStream());
    });
    vi.stubGlobal("fetch", fetcher);
    const { result, unmount } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.submit("Book a meeting");
    });
    expect(result.current.pending).not.toBeNull();
    expect(result.current.messages).toEqual([
      expect.objectContaining({ content: "Book a meeting", status: "unconfirmed" }),
    ]);
    await act(async () => {
      await result.current.submit("", true);
    });
    expect(result.current.messages).toEqual([
      expect.objectContaining({ id: "u", content: "Book a meeting", status: "sent" }),
    ]);
    expect(submissions).toHaveLength(2);
    expect(submissions[0]).toBe(submissions[1]);
    unmount();
  });
  it("keeps an optimistic correction visible when the previous run refreshes history", async () => {
    let events!: ReadableStreamDefaultController<Uint8Array>;
    let accept!: (response: Response) => void;
    const response = new Promise<Response>((resolve) => {
      accept = resolve;
    });
    let historyReads = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes("history")) {
        historyReads++;
        return json({ messages: [], next_cursor: null });
      }
      if (url === "/api/chat/messages") return response;
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            events = controller;
          },
        }),
      );
    });
    sessionStorage.setItem("daylight-run:1", "previous");
    vi.stubGlobal("fetch", fetcher);
    const { result, unmount } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let submission!: Promise<boolean>;
    act(() => {
      submission = result.current.submit("Use a later time");
    });
    const optimisticId = result.current.messages[0].id;
    await act(async () => {
      events.enqueue(new TextEncoder().encode("id: 1\nevent: run.completed\ndata: {}\n\n"));
    });
    await waitFor(() => expect(historyReads).toBe(2));
    expect(result.current.messages).toEqual([
      expect.objectContaining({ id: optimisticId, content: "Use a later time", status: "sending" }),
    ]);
    await act(async () => {
      accept(json({ run_id: "next", message_id: "correction", revision: 2 }));
      await submission;
    });
    expect(result.current.messages).toEqual([
      expect.objectContaining({ id: "correction", content: "Use a later time", status: "sent" }),
    ]);
    unmount();
  });
  it("resumes a remembered run without posting a message", async () => {
    sessionStorage.setItem("daylight-run:1", "remembered");
    const fetcher = vi.fn(async (url: string) =>
      url.includes("history")
        ? json({ messages: [], next_cursor: null })
        : stream('id: 1\nevent: run.waiting\ndata: {"message":"Please confirm the booking."}\n\n'),
    );
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.status).toBe("Please confirm the booking."));
    expect(fetcher.mock.calls.some(([url]) => url.includes("remembered/events?after=0"))).toBe(true);
    expect(fetcher.mock.calls.some(([url]) => url === "/api/chat/messages")).toBe(false);
  });
  it("reconnects from the last event ID without resubmitting the message", async () => {
    let eventReads = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes("history")) return json({ messages: [], next_cursor: null });
      if (url === "/api/chat/messages") return json({ run_id: "r", message_id: "u", revision: 1 });
      if (url.includes("/events")) {
        eventReads++;
        return eventReads === 1
          ? stream('id: 7\nevent: tool.status\ndata: {"text":"Checking availability"}\n\n')
          : stream('id: 8\nevent: run.waiting\ndata: {"message":"Which time works?"}\n\n');
      }
      return json({ id: "r", status: "running" });
    });
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.submit("Find a time");
    });
    await waitFor(() => expect(result.current.status).toBe("Which time works?"), { timeout: 2500 });
    const reconnected = fetcher.mock.calls.find(([url]) => url.includes("events?after=7"));
    expect(reconnected).toBeDefined();
    expect(fetcher.mock.calls.filter(([url]) => url === "/api/chat/messages")).toHaveLength(1);
  });
  it("explains an already-dispatched action after Stop and keeps observing its outcome", async () => {
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes("history")) return json({ messages: [], next_cursor: null });
      if (url === "/api/chat/messages") return json({ run_id: "r", message_id: "u", revision: 1 });
      if (url.endsWith("/stop")) return json({ status: "running", dispatch_committed: true });
      return new Response(new ReadableStream());
    });
    vi.stubGlobal("fetch", fetcher);
    const { result, unmount } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.submit("Book a meeting");
    });
    await act(async () => {
      await result.current.stop();
    });
    expect(result.current.status).toContain("already dispatched may still complete");
    expect(result.current.run).toBe("r");
    unmount();
  });
});

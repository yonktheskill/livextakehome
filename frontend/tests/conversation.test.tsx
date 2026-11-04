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
  it("reloads after an initial history failure and reconnects the saved run", async () => {
    sessionStorage.setItem("daylight-run:1", "saved");
    let reads = 0;
    const fetcher = vi.fn(async (url: string) => {
      if (url.includes("history")) {
        if (++reads === 1) return json({ detail: "History unavailable" }, 503);
        return json({ messages: [], next_cursor: null });
      }
      return new Response(new ReadableStream());
    });
    vi.stubGlobal("fetch", fetcher);
    const { result, unmount } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.error).toBe("History unavailable"));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBe("");
    expect(result.current.run).toBe("saved");
    expect(fetcher.mock.calls.some(([url]) => url.includes("saved/events"))).toBe(true);
    unmount();
  });
  it("restores uncertain delivery after reload, isolates users, and requires explicit retry", async () => {
    const posts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (url.includes("history")) return json({ messages: [], next_cursor: null });
        if (url === "/api/chat/messages") {
          posts.push(init!.body as string);
          if (posts.length === 1) throw new TypeError("Lost response");
          return json({ run_id: "r", message_id: "u", revision: 1 });
        }
        return new Response(new ReadableStream());
      }),
    );
    const first = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    await act(async () => {
      await first.result.current.submit("Book a meeting");
    });
    first.unmount();
    const other = renderHook(() => useConversation(2, authorized));
    await waitFor(() => expect(other.result.current.loading).toBe(false));
    expect(other.result.current.pending).toBeNull();
    other.unmount();
    const restored = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(restored.result.current.loading).toBe(false));
    expect(restored.result.current.pending?.content).toBe("Book a meeting");
    expect(posts).toHaveLength(1);
    await act(async () => {
      expect(await restored.result.current.submit("New request")).toBe(false);
    });
    await act(async () => {
      await restored.result.current.submit("", true);
    });
    expect(posts).toEqual([posts[0], posts[0]]);
    expect(sessionStorage.getItem("daylight-pending:1")).toBeNull();
    restored.unmount();
  });
  it("does not POST when the request identity cannot be persisted", async () => {
    const fetcher = vi.fn(async () => json({ messages: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetcher);
    const { result, unmount } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Denied");
    });
    await act(async () => {
      expect(await result.current.submit("Hello")).toBe(false);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.error).toContain("session storage is unavailable");
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
    const confirmation = {
      id: "confirmation",
      role: "assistant",
      content: "Shall I create a reusable 30-minute meeting type?",
      status: "final",
      created_at: "2026-11-05T00:00:00Z",
    };
    const fetcher = vi.fn(async (url: string) =>
      url.includes("history")
        ? json({ messages: [confirmation], next_cursor: null })
        : stream(
            `id: 1\nevent: message.delta\ndata: ${JSON.stringify({ message_id: confirmation.id, text: confirmation.content })}\n\nid: 2\nevent: run.waiting\ndata: ${JSON.stringify({ message: confirmation.content })}\n\n`,
          ),
    );
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(sessionStorage.getItem("daylight-run:1")).toBeNull());
    expect(result.current.status).toBe("");
    expect(result.current.messages).toEqual([confirmation]);
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
          : stream("id: 8\nevent: run.completed\ndata: {}\n\n");
      }
      return json({ id: "r", status: "running" });
    });
    vi.stubGlobal("fetch", fetcher);
    const { result } = renderHook(() => useConversation(1, authorized));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => {
      await result.current.submit("Find a time");
    });
    await waitFor(() => expect(eventReads).toBe(2), { timeout: 2500 });
    await waitFor(() => expect(result.current.run).toBeNull());
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

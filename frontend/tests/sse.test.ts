import { describe, expect, it } from "vitest";
import { SSEParser, terminalEvents, type StreamEvent } from "../lib/sse";
describe("durable SSE frames", () => {
  it("parses CRLF split at every character, comments, event IDs, multiline data and UTF-8", () => {
    const events: StreamEvent[] = [];
    const parser = new SSEParser((e) => events.push(e));
    const frame =
      ': heartbeat\r\nid: 12\r\nevent: message.delta\r\ndata: {"message_id":"m1",\r\ndata: "text":"Hello 🌞"}\r\n\r\n';
    for (const char of frame) parser.feed(char);
    expect(events).toEqual([{ id: 12, type: "message.delta", data: { message_id: "m1", text: "Hello 🌞" } }]);
  });
  it("handles multiple frames and ignores frames without event IDs", () => {
    const events: StreamEvent[] = [];
    const parser = new SSEParser((e) => events.push(e));
    parser.feed("data: {}\n\nid: 2\nevent: run.completed\ndata: {}\n\n");
    expect(events).toEqual([{ id: 2, type: "run.completed", data: {} }]);
  });
  it("fails closed on corrupt events so a reconnect cannot silently skip them", () => {
    const parser = new SSEParser(() => {});
    expect(() => parser.feed("id: 1\ndata: broken\n\n")).toThrow();
  });
  it("does not dispatch an incomplete frame", () => {
    const events: StreamEvent[] = [];
    const parser = new SSEParser((e) => events.push(e));
    parser.feed('id: 1\nevent: message.delta\ndata: {"text":"a"}\n');
    expect(events).toHaveLength(0);
    parser.feed("\n");
    expect(events).toHaveLength(1);
  });
  it("treats waiting and each completion state as terminal", () => {
    expect([...terminalEvents]).toEqual(["run.waiting", "run.completed", "run.failed", "run.interrupted"]);
  });
});

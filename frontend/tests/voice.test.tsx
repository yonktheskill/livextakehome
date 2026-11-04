import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVoice } from "../lib/use-voice";
import { useConversation } from "../lib/use-conversation";
import * as playback from "../lib/voice-playback";

const json = (value: unknown) => new Response(JSON.stringify(value));
const unauthorized = vi.fn();
const track = { stop: vi.fn(), enabled: true };
const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
class Peer {
  static latest: Peer;
  constructor() {
    Peer.latest = this;
  }
  close = vi.fn();
  addTrack = vi.fn();
  createDataChannel = vi.fn();
  createOffer = vi.fn(async () => ({ type: "offer", sdp: "offer-sdp" }));
  setLocalDescription = vi.fn(async () => {});
  setRemoteDescription = vi.fn(async () => {});
  ontrack = null;
  onconnectionstatechange = null;
}

describe("voice connection", () => {
  beforeEach(() => {
    sessionStorage.clear();
    track.enabled = true;
    track.stop.mockClear();
    vi.stubGlobal("RTCPeerConnection", Peer);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Reflect.deleteProperty(navigator, "mediaDevices");
  });

  function setup(stopFails = false) {
    let events!: ReadableStreamDefaultController<Uint8Array>;
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/voice/config")) return json({ enabled: true });
      if (url === "/api/voice/sessions") return json({ id: "voice-1", sdp: "answer-sdp" });
      if (url.includes("/events"))
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              events = controller;
            },
          }),
        );
      if (url.endsWith("/mute")) return json(JSON.parse(init!.body as string));
      if (url.endsWith("/playback-finished")) return json({ closed: true });
      if (url.endsWith("/stop"))
        return stopFails ? new Response("", { status: 503 }) : json({ status: "closed" });
      throw new Error(`Unexpected URL ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    const onEvent = vi.fn();
    const refresh = vi.fn(async () => {});
    const hook = renderHook(() => useVoice(onEvent, refresh, unauthorized));
    return {
      ...hook,
      fetcher,
      onEvent,
      refresh,
      close: () => events.close(),
      emit: (id: number, type: string, data: unknown) =>
        events.enqueue(
          new TextEncoder().encode(`id: ${id}\nevent: ${type}\ndata: ${JSON.stringify(data)}\n\n`),
        ),
    };
  }

  it("drains final transcript replacements after the terminal state before refreshing history", async () => {
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    await act(async () => {
      voice.emit(1, "voice.state", { status: "closed" });
      voice.emit(2, "message.upsert", {
        message: { id: "u", content: "Final caption", status: "voice_recorded" },
      });
      voice.close();
    });
    expect(voice.onEvent).toHaveBeenCalledTimes(2);
    expect(voice.onEvent.mock.calls[1][0].data.message.content).toBe("Final caption");
    expect(voice.refresh).toHaveBeenCalledOnce();
    expect(voice.result.current.state).toBe("idle");
    voice.unmount();
  });

  it("negotiates WebRTC without exposing credentials, follows durable captions, and flushes before ending", async () => {
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    expect(Peer.latest.setRemoteDescription).toHaveBeenCalledWith({ type: "answer", sdp: "answer-sdp" });
    const body = JSON.parse(
      voice.fetcher.mock.calls.find(([url]) => url === "/api/voice/sessions")![1]!.body as string,
    );
    expect(body.sdp).toBe("offer-sdp");
    expect(body.client_request_id).toMatch(/^[a-f0-9-]{36}$/);
    await act(async () => {
      voice.emit(1, "message.upsert", { message: { id: "u", content: "Book", role: "user" } });
      voice.emit(1, "message.upsert", { message: { id: "u", content: "Book", role: "user" } });
      voice.emit(2, "message.upsert", { message: { id: "a", content: "Sure", role: "assistant" } });
    });
    expect(voice.onEvent).toHaveBeenCalledTimes(2);
    await act(async () => {
      expect(await voice.result.current.end()).toBe(true);
    });
    expect(track.stop).toHaveBeenCalled();
    expect(Peer.latest.close).toHaveBeenCalled();
    expect(voice.refresh).toHaveBeenCalledOnce();
    expect(voice.result.current.state).toBe("idle");
    voice.unmount();
  });

  it("mutes local audio as well as provider input and releases media on unmount", async () => {
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    await act(async () => {
      await voice.result.current.toggleMute();
    });
    expect(track.enabled).toBe(false);
    expect(voice.result.current.muted).toBe(true);
    await act(async () => {
      await voice.result.current.toggleMute();
    });
    expect(track.enabled).toBe(true);
    voice.unmount();
    expect(track.stop).toHaveBeenCalled();
    expect(voice.fetcher).toHaveBeenLastCalledWith(
      "/api/voice/sessions/voice-1/stop",
      expect.objectContaining({ keepalive: true }),
    );
  });

  it("does not permit switching to typed chat when transcript flush fails", async () => {
    const voice = setup(true);
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    await act(async () => {
      expect(await voice.result.current.end()).toBe(false);
    });
    expect(voice.result.current.state).toBe("failed");
    expect(voice.result.current.active).toBe(true);
    expect(track.stop).toHaveBeenCalled();
    expect(voice.refresh).not.toHaveBeenCalled();
    voice.unmount();
  });

  it("shows microphone permission errors without creating a paid session", async () => {
    const voice = setup();
    vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(
      new DOMException("Denied", "NotAllowedError"),
    );
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    expect(voice.result.current.error).toContain("Microphone access was denied");
    expect(voice.fetcher.mock.calls.some(([url]) => url === "/api/voice/sessions")).toBe(false);
    voice.unmount();
  });

  it("keeps the call connected through confirmation playback and closes after quiet audio", async () => {
    const sample = vi.fn(() => true);
    vi.spyOn(playback, "monitorVoiceAudio").mockReturnValue({
      sample,
      resume: vi.fn(async () => {}),
      close: vi.fn(),
    });
    vi.spyOn(HTMLMediaElement.prototype, "paused", "get").mockReturnValue(false);
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    const ontrack = Peer.latest.ontrack as unknown as (event: { streams: unknown[] }) => void;
    act(() => ontrack({ streams: [stream] }));
    vi.useFakeTimers();
    await act(async () => {
      voice.emit(1, "voice.end_requested", { request_id: "finish-1", reason: "booking_complete" });
    });
    expect(voice.result.current.state).toBe("ending");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    expect(voice.fetcher.mock.calls.some(([url]) => url.endsWith("/playback-finished"))).toBe(false);
    expect(Peer.latest.close).not.toHaveBeenCalled();
    sample.mockReturnValue(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(voice.fetcher.mock.calls.filter(([url]) => url.endsWith("/playback-finished"))).toHaveLength(1);
    // Wait for the server's final transcript/state drain before releasing transport.
    await act(async () => {
      voice.emit(2, "voice.state", { status: "closed" });
      voice.close();
    });
    expect(track.stop).toHaveBeenCalled();
    expect(voice.result.current.state).toBe("idle");
    voice.unmount();
  });

  it("cancels an automatic hangup when the backend accepts continued conversation", async () => {
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    vi.useFakeTimers();
    await act(async () => {
      voice.emit(1, "voice.end_requested", { request_id: "finish-1", reason: "booking_complete" });
      voice.emit(2, "voice.end_cancelled", { request_id: "finish-1" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(voice.fetcher.mock.calls.some(([url]) => url.endsWith("/playback-finished"))).toBe(false);
    expect(voice.result.current.state).toBe("active");
    expect(track.enabled).toBe(true);
    voice.unmount();
  });

  it("mutes microphone input immediately after an explicit spoken hangup request", async () => {
    const voice = setup();
    await waitFor(() => expect(voice.result.current.enabled).toBe(true));
    await act(async () => {
      await voice.result.current.start();
    });
    vi.useFakeTimers();
    await act(async () => {
      voice.emit(1, "voice.end_requested", { request_id: "finish-1", reason: "user_requested" });
    });
    expect(track.enabled).toBe(false);
    expect(voice.result.current.muted).toBe(true);
    expect(voice.result.current.state).toBe("ending");
    voice.unmount();
  });
});

it("does not mistake transcript delivery gaps or ongoing audio for finished playback", () => {
  const drain = new playback.VoicePlaybackDrain(0);
  expect(drain.ready(5000, true)).toBe(false);
  expect(drain.ready(7000, true)).toBe(false);
  expect(drain.ready(8500, false)).toBe(false);
  drain.caption(8600);
  expect(drain.ready(10000, false)).toBe(false);
  expect(drain.ready(10600, false)).toBe(true);
  const unsupported = new playback.VoicePlaybackDrain(0);
  expect(unsupported.ready(10000, undefined)).toBe(false);
  expect(unsupported.ready(45000, undefined)).toBe(true);
});

it("merges overlapping voice speakers into the existing history without duplicate or stale bubbles", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      json({
        messages: [
          {
            id: "typed",
            role: "user",
            content: "Tomorrow",
            status: "final",
            created_at: "2026-01-01T00:00:00Z",
          },
        ],
        next_cursor: null,
      }),
    ),
  );
  const chat = renderHook(() => useConversation("voice-test", unauthorized));
  await waitFor(() => expect(chat.result.current.loading).toBe(false));
  const message = (id: string, content: string, version: number, time: string) => ({
    id,
    role: id === "u" ? "user" : "assistant",
    content,
    version,
    status: "partial",
    source: "voice",
    created_at: time,
  });
  act(() => {
    chat.result.current.applyVoiceEvent({
      id: 1,
      type: "message.upsert",
      data: { message: message("u", "Book", 1, "2026-01-01T00:00:01Z") },
    });
    chat.result.current.applyVoiceEvent({
      id: 2,
      type: "message.upsert",
      data: { message: message("a", "Sure", 1, "2026-01-01T00:00:02Z") },
    });
    chat.result.current.applyVoiceEvent({
      id: 3,
      type: "message.upsert",
      data: { message: message("u", "Book tomorrow", 2, "2026-01-01T00:00:01Z") },
    });
    chat.result.current.applyVoiceEvent({
      id: 4,
      type: "message.upsert",
      data: { message: message("u", "Book", 1, "2026-01-01T00:00:01Z") },
    });
  });
  expect(chat.result.current.messages.map((message) => [message.id, message.content])).toEqual([
    ["typed", "Tomorrow"],
    ["u", "Book tomorrow"],
    ["a", "Sure"],
  ]);
  chat.unmount();
  vi.unstubAllGlobals();
});

"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { SSEParser, type StreamEvent } from "./sse";
import { monitorVoiceAudio, VoicePlaybackDrain } from "./voice-playback";

type State = "idle" | "connecting" | "active" | "ending" | "reconnecting" | "stopping" | "failed";
type Resources = {
  id?: string;
  peer: RTCPeerConnection;
  stream: MediaStream;
  audio: HTMLAudioElement;
  events?: AbortController;
  monitor?: ReturnType<typeof monitorVoiceAudio>;
  ending?: { id: string; drain: VoicePlaybackDrain; timer: ReturnType<typeof setInterval> };
};
function release(resources: Resources, abortEvents = true) {
  if (resources.ending) clearInterval(resources.ending.timer);
  resources.monitor?.close();
  resources.monitor = undefined;
  if (abortEvents) resources.events?.abort();
  resources.peer.ontrack = null;
  resources.peer.onconnectionstatechange = null;
  resources.peer.close();
  resources.stream.getTracks().forEach((track) => track.stop());
  resources.audio.pause();
  resources.audio.srcObject = null;
}
/** The backend owns transcript identity/persistence; WebRTC carries audio only. */
export function useVoice(
  onEvent: (event: StreamEvent) => void,
  refreshHistory: () => Promise<void>,
  onUnauthorized: () => void,
) {
  const [enabled, setEnabled] = useState(false);
  const [state, setState] = useState<State>("idle");
  const [muted, setMuted] = useState(false);
  const [hasSession, setHasSession] = useState(false);
  const [muting, setMuting] = useState(false);
  const [error, setError] = useState("");
  const [playbackBlocked, setPlaybackBlocked] = useState(false);
  const current = useRef<Resources | null>(null);
  const generation = useRef(0);
  const starting = useRef(false);
  const mounted = useRef(true);
  const callbacks = useRef({ onEvent, refreshHistory, onUnauthorized });
  useEffect(() => {
    callbacks.current = { onEvent, refreshHistory, onUnauthorized };
  }, [onEvent, refreshHistory, onUnauthorized]);
  const report = useCallback((failure: unknown) => {
    if (failure instanceof ApiError && failure.status === 401) callbacks.current.onUnauthorized();
    if (mounted.current)
      setError(failure instanceof Error ? failure.message : "Voice connection failed. Try again.");
  }, []);
  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    void api<{ enabled: boolean }>("/voice/config")
      .then((config) => {
        if (!cancelled) setEnabled(config.enabled === true);
      })
      .catch(() => {
        /* Voice is optional; typed chat remains available. */
      });
    const abandon = () => {
      generation.current++;
      const resources = current.current;
      current.current = null;
      if (!resources) return;
      release(resources);
      if (resources.id)
        void fetch(`/api/voice/sessions/${encodeURIComponent(resources.id)}/stop`, {
          method: "POST",
          credentials: "same-origin",
          keepalive: true,
        }).catch(() => {});
    };
    window.addEventListener("pagehide", abandon);
    return () => {
      cancelled = true;
      mounted.current = false;
      window.removeEventListener("pagehide", abandon);
      abandon();
    };
  }, []);
  const end = useCallback(async () => {
    const resources = current.current;
    if (!resources) return true;
    generation.current++;
    setState("stopping");
    // Stop the microphone and speaker immediately, then flush canonical transcripts.
    release(resources);
    try {
      if (resources.id)
        await api(`/voice/sessions/${encodeURIComponent(resources.id)}/stop`, { method: "POST" });
      current.current = null;
      if (mounted.current) setHasSession(false);
      await callbacks.current.refreshHistory();
      if (mounted.current) {
        setState("idle");
        setMuted(false);
        setPlaybackBlocked(false);
      }
      return true;
    } catch (failure) {
      report(failure);
      if (mounted.current) setState("failed");
      // Retain the ID so End can retry before a typed request is accepted.
      return false;
    }
  }, [report]);
  const follow = useCallback(
    async (resources: Resources) => {
      const abort = new AbortController();
      resources.events = abort;
      let cursor = 0;
      let closed = false;
      let failed = false;
      const receive = (event: StreamEvent) => {
        if (abort.signal.aborted || event.id <= cursor) return;
        callbacks.current.onEvent(event);
        cursor = event.id;
        if (event.type === "message.upsert" && resources.ending) {
          const message = event.data.message as { role?: string } | undefined;
          if (message?.role === "assistant") resources.ending.drain.caption(performance.now());
        }
        if (
          event.type === "voice.end_cancelled" &&
          resources.ending &&
          resources.ending.id === event.data.request_id
        ) {
          clearInterval(resources.ending.timer);
          resources.ending = undefined;
          if (mounted.current) setState("active");
        }
        if (event.type === "voice.end_requested" && typeof event.data.request_id === "string") {
          if (resources.ending) clearInterval(resources.ending.timer);
          const id = event.data.request_id;
          const userRequested = event.data.reason === "user_requested";
          const drain = new VoicePlaybackDrain(performance.now(), userRequested ? 10000 : undefined);
          if (userRequested) {
            resources.stream.getAudioTracks().forEach((track) => {
              track.enabled = false;
            });
            if (mounted.current) setMuted(true);
          }
          if (mounted.current) setState("ending");
          const timer = setInterval(() => {
            const audible = resources.audio.paused ? false : resources.monitor?.sample();
            if (!drain.ready(performance.now(), audible)) return;
            clearInterval(timer);
            // The caller asked to disconnect. Release media after the farewell even
            // if the acknowledgment or terminal event is delayed/lost.
            if (userRequested) {
              release(resources, false);
              if (mounted.current) setState("stopping");
            }
            void api<{ closed: boolean }>(
              `/voice/sessions/${encodeURIComponent(resources.id!)}/playback-finished`,
              {
                method: "POST",
                body: JSON.stringify({ request_id: id }),
              },
            )
              .then((result) => {
                if (current.current !== resources || resources.ending?.id !== id) return;
                if (result.closed) {
                  // Closure is persisted before this response. Refresh canonical captions
                  // instead of depending on a second, possibly interrupted SSE delivery.
                  release(resources);
                  current.current = null;
                  if (mounted.current) {
                    setHasSession(false);
                    setState("idle");
                    setMuted(false);
                    setPlaybackBlocked(false);
                  }
                  void callbacks.current.refreshHistory().catch(report);
                } else if (userRequested) {
                  void end();
                } else {
                  resources.ending = undefined;
                  if (mounted.current) setState("active");
                }
              })
              .catch((failure) => {
                if (current.current === resources) {
                  report(failure);
                  void end();
                }
              });
          }, 250);
          resources.ending = { id, drain, timer };
        }
        if (event.type === "voice.state") {
          if (event.data.status === "active" && !resources.ending) setState("active");
          if (["closed", "failed"].includes(String(event.data.status))) {
            closed = true;
            failed = event.data.status === "failed";
            if (event.data.status === "failed")
              report(new Error(String(event.data.message || "Voice session ended unexpectedly.")));
            // Drain the final response batch: a terminal notice can precede a final upsert.
            release(resources, false);
            if (mounted.current) setState("stopping");
          }
        }
      };
      for (let attempt = 0; attempt < 6 && !abort.signal.aborted; attempt++) {
        try {
          const response = await fetch(
            `/api/voice/sessions/${encodeURIComponent(resources.id!)}/events?after=${cursor}`,
            {
              credentials: "same-origin",
              cache: "no-store",
              signal: abort.signal,
              headers: { Accept: "text/event-stream", "Last-Event-ID": String(cursor) },
            },
          );
          if (!response.ok)
            throw new ApiError(response.status, `Unable to follow voice transcripts (${response.status}).`);
          if (!response.body) throw new Error("Voice transcript streaming is unavailable.");
          if (mounted.current) setState(resources.ending ? "ending" : "active");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          const parser = new SSEParser(receive);
          try {
            while (!abort.signal.aborted) {
              const { done, value } = await reader.read();
              if (done) {
                parser.feed(decoder.decode());
                break;
              }
              parser.feed(decoder.decode(value, { stream: true }));
            }
          } finally {
            await reader.cancel().catch(() => {});
            reader.releaseLock();
          }
          if (abort.signal.aborted) return;
        } catch (failure) {
          if (abort.signal.aborted) return;
          if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) {
            report(failure);
            break;
          }
        }
        if (closed) {
          release(resources);
          if (current.current === resources) {
            current.current = null;
            if (mounted.current) setHasSession(false);
          }
          if (mounted.current) setState(failed ? "failed" : "idle");
          void callbacks.current.refreshHistory().catch(report);
          return;
        }
        if (mounted.current) setState("reconnecting");
        await new Promise<void>((resolve) => {
          const finish = () => {
            clearTimeout(timer);
            abort.signal.removeEventListener("abort", finish);
            resolve();
          };
          const timer = setTimeout(finish, Math.min(500 * 2 ** attempt, 8000));
          abort.signal.addEventListener("abort", finish, { once: true });
        });
      }
      if (!abort.signal.aborted) {
        report(new Error("Voice transcript connection was lost. Voice has stopped; retry to reconnect."));
        await end();
      }
    },
    [end, report],
  );
  async function start() {
    if (!enabled || starting.current || current.current || state === "connecting" || state === "stopping")
      return;
    if (!navigator.mediaDevices?.getUserMedia || typeof RTCPeerConnection === "undefined") {
      report(new Error("Voice needs a supported browser on HTTPS or localhost."));
      return;
    }
    starting.current = true;
    const token = ++generation.current;
    setState("connecting");
    setError("");
    setMuted(false);
    let resources: Resources | undefined;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
        video: false,
      });
      if (!mounted.current || token !== generation.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const peer = new RTCPeerConnection();
      const audio = document.createElement("audio");
      audio.autoplay = true;
      resources = { peer, audio, stream };
      current.current = resources;
      setHasSession(true);
      peer.ontrack = (event) => {
        audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
        resources!.monitor?.close();
        resources!.monitor = monitorVoiceAudio(audio.srcObject as MediaStream);
        void audio.play().catch(() => {
          if (mounted.current) setPlaybackBlocked(true);
        });
      };
      peer.onconnectionstatechange = () => {
        if (peer.connectionState === "failed") {
          report(new Error("Audio connection failed. End voice and try again."));
          void end();
        }
      };
      stream.getAudioTracks().forEach((track) => peer.addTrack(track, stream));
      peer.createDataChannel("oai-events");
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const session = await api<{ id: string; sdp: string }>("/voice/sessions", {
        method: "POST",
        body: JSON.stringify({ sdp: offer.sdp, client_request_id: crypto.randomUUID() }),
      });
      resources.id = session.id;
      if (!mounted.current || token !== generation.current) {
        release(resources);
        await api(`/voice/sessions/${encodeURIComponent(session.id)}/stop`, { method: "POST" });
        return;
      }
      await peer.setRemoteDescription({ type: "answer", sdp: session.sdp });
      setState("active");
      void follow(resources);
    } catch (failure) {
      if (resources) {
        release(resources);
        if (resources.id)
          await api(`/voice/sessions/${encodeURIComponent(resources.id)}/stop`, { method: "POST" }).catch(
            () => {},
          );
        if (current.current === resources) {
          current.current = null;
          if (mounted.current) setHasSession(false);
        }
      }
      if (mounted.current && token === generation.current) {
        report(
          failure instanceof DOMException && failure.name === "NotAllowedError"
            ? new Error("Microphone access was denied. Allow it in your browser and try again.")
            : failure,
        );
        setState("failed");
      }
    } finally {
      starting.current = false;
    }
  }
  async function toggleMute() {
    const resources = current.current;
    if (!resources?.id || muting) return;
    const next = !muted;
    setMuting(true);
    if (next)
      resources.stream.getAudioTracks().forEach((track) => {
        track.enabled = false;
      });
    try {
      await api(`/voice/sessions/${encodeURIComponent(resources.id)}/mute`, {
        method: "POST",
        body: JSON.stringify({ muted: next }),
      });
      resources.stream.getAudioTracks().forEach((track) => {
        track.enabled = !next;
      });
      setMuted(next);
    } catch (failure) {
      resources.stream.getAudioTracks().forEach((track) => {
        track.enabled = !muted;
      });
      report(failure);
    } finally {
      if (mounted.current) setMuting(false);
    }
  }
  return {
    enabled,
    state,
    muted,
    muting,
    error,
    playbackBlocked,
    start,
    end,
    toggleMute,
    active: ["connecting", "active", "ending", "reconnecting", "stopping"].includes(state) || hasSession,
    resumePlayback: async () => {
      try {
        await current.current?.monitor?.resume();
        await current.current?.audio.play();
        setPlaybackBlocked(false);
      } catch (failure) {
        report(failure);
      }
    },
  };
}

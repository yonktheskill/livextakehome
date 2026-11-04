"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type History, type Message, type Receipt } from "./api";
import { SSEParser, terminalEvents, type StreamEvent } from "./sse";
export type { Receipt } from "./api";
type Submission = { content: string; client_request_id: string };
type Accepted = { run_id: string; message_id: string; revision: number };
export function useConversation(userId: string | number, onUnauthorized: () => void) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [run, setRun] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"idle" | "live" | "reconnecting" | "paused">("idle");
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [pending, setPending] = useState<Submission | null>(null);
  const [optimisticMessage, setOptimisticMessage] = useState<Message | null>(null);
  // Keep unconfirmed delivery visible even when an active run refreshes history.
  const visibleMessages = useMemo(
    () => (optimisticMessage ? [...messages, optimisticMessage] : messages),
    [messages, optimisticMessage],
  );
  const controller = useRef<AbortController | null>(null);
  const sendLock = useRef(false);
  const lastId = useRef(0);
  const activeRun = useRef<string | null>(null);
  const mounted = useRef(true);
  const key = `daylight-run:${userId}`;
  const authRef = useRef(onUnauthorized);
  useEffect(() => {
    authRef.current = onUnauthorized;
  }, [onUnauthorized]);
  const report = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      authRef.current();
      return;
    }
    setError(e instanceof Error ? e.message : "Something went wrong. Please try again.");
  }, []);
  const history = useCallback(async (before?: string) => {
    const runAtRead = activeRun.current;
    const page = await api<History>(`/chat/history${before ? `?before=${encodeURIComponent(before)}` : ""}`);
    if (!mounted.current || runAtRead !== activeRun.current) return;
    setMessages((previous) =>
      before
        ? [...page.messages.filter((m) => !previous.some((p) => p.id === m.id)), ...previous]
        : page.messages,
    );
    setCursor(page.next_cursor);
    if (page.receipts)
      setReceipts((previous) => {
        const combined = before ? [...previous, ...page.receipts!] : page.receipts!;
        return [...new Map(combined.map((r) => [r.action_id, r])).values()];
      });
  }, []);
  const connect = useCallback(
    (runId: string, fresh = true) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      activeRun.current = runId;
      setRun(runId);
      if (fresh) lastId.current = 0;
      try {
        sessionStorage.setItem(key, runId);
      } catch {
        /* Storage can be unavailable. */
      }
      const replayed = new Set<string>();
      let terminal = false;
      const event = (frame: StreamEvent) => {
        if (abort.signal.aborted || frame.id <= lastId.current) return;
        lastId.current = frame.id;
        const data = frame.data;
        if (
          frame.type === "message.delta" &&
          typeof data.message_id === "string" &&
          typeof data.text === "string"
        ) {
          const id = data.message_id,
            text = data.text;
          const reset = fresh && !replayed.has(id);
          replayed.add(id);
          setMessages((previous) => {
            const found = previous.some((m) => m.id === id);
            return found
              ? previous.map((m) => (m.id === id ? { ...m, content: (reset ? "" : m.content) + text } : m))
              : [
                  ...previous,
                  {
                    id,
                    role: "assistant",
                    content: text,
                    status: "streaming",
                    created_at:
                      typeof data.created_at === "string" ? data.created_at : new Date().toISOString(),
                  },
                ];
          });
        }
        if (frame.type === "tool.status" && typeof data.text === "string") setStatus(data.text);
        if (
          frame.type === "action.result" &&
          typeof data.action_id === "string" &&
          typeof data.summary === "string" &&
          data.kind === "create_booking"
        ) {
          setReceipts((previous) => {
            const existing = previous.find((r) => r.action_id === data.action_id);
            const receipt = {
              ...data,
              created_at:
                typeof data.created_at === "string"
                  ? data.created_at
                  : (existing?.created_at ?? new Date().toISOString()),
            } as Receipt;
            return [...previous.filter((r) => r.action_id !== receipt.action_id), receipt];
          });
        }
        if (terminalEvents.has(frame.type)) {
          terminal = true;
          activeRun.current = null;
          setRun(null);
          setStreamState("idle");
          setStatus(
            frame.type === "run.interrupted"
              ? "Stopped at a safe checkpoint. Completed actions remain in your history."
              : "",
          );
          if (frame.type === "run.failed")
            setError(
              typeof data.message === "string"
                ? data.message
                : "The assistant could not complete this request.",
            );
          try {
            sessionStorage.removeItem(key);
          } catch {}
          void history().catch(report);
          abort.abort();
        }
      };
      void (async () => {
        for (let attempt = 0; attempt < 6 && !abort.signal.aborted; attempt++) {
          setStreamState(attempt ? "reconnecting" : "live");
          try {
            const response = await fetch(
              `/api/runs/${encodeURIComponent(runId)}/events?after=${lastId.current}`,
              {
                credentials: "same-origin",
                cache: "no-store",
                signal: abort.signal,
                headers: { Accept: "text/event-stream", "Last-Event-ID": String(lastId.current) },
              },
            );
            if (!response.ok)
              throw new ApiError(response.status, `Unable to connect to the response (${response.status}).`);
            if (!response.body) throw new Error("Streaming is unavailable. Reconnect to check your request.");
            setStreamState("live");
            const reader = response.body.getReader(),
              decoder = new TextDecoder();
            const parser = new SSEParser(event);
            try {
              while (!abort.signal.aborted) {
                const { value, done } = await reader.read();
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
            if (terminal || abort.signal.aborted) return;
            // A completed run can close without another frame when reconnecting at its tail.
            const state = await api<{ status: string }>(`/runs/${encodeURIComponent(runId)}`);
            if (
              ["succeeded", "failed", "interrupted", "waiting_for_user", "needs_reconciliation"].includes(
                state.status,
              )
            ) {
              event({
                id: lastId.current + 1,
                type: ["failed", "needs_reconciliation"].includes(state.status)
                  ? "run.failed"
                  : state.status === "interrupted"
                    ? "run.interrupted"
                    : "run.completed",
                data:
                  state.status === "needs_reconciliation"
                    ? {
                        message:
                          "An action needs reconciliation. Its outcome is uncertain; do not repeat it until it has been checked.",
                      }
                    : {},
              });
              return;
            }
          } catch (e) {
            if (abort.signal.aborted) return;
            if (e instanceof ApiError && e.status === 401) {
              authRef.current();
              return;
            }
            if (e instanceof ApiError && [403, 404].includes(e.status)) {
              report(e);
              break;
            }
          }
          if (abort.signal.aborted) return;
          setStreamState("reconnecting");
          await new Promise<void>((resolve) => {
            const finish = () => {
              clearTimeout(timer);
              abort.signal.removeEventListener("abort", finish);
              resolve();
            };
            const timer = setTimeout(finish, Math.min(1000 * 2 ** attempt, 10000));
            abort.signal.addEventListener("abort", finish, { once: true });
          });
        }
        if (!abort.signal.aborted) {
          setStreamState("paused");
          setStatus("Connection paused. Your request may still be running. Reconnect to check its progress.");
        }
      })();
    },
    [history, key, report],
  );
  useEffect(() => {
    mounted.current = true;
    void (async () => {
      try {
        await history();
        if (!mounted.current) return;
        let saved: string | null = null;
        try {
          saved = sessionStorage.getItem(key);
        } catch {}
        if (saved) connect(saved);
      } catch (e) {
        if (mounted.current) report(e);
      } finally {
        if (mounted.current) setLoading(false);
      }
    })();
    return () => {
      mounted.current = false;
      controller.current?.abort();
    };
  }, [history, connect, key, report]);
  async function submit(content: string, retry = false) {
    if (sendLock.current || loading || (retry ? !pending : !!pending || !content.trim())) return false;
    sendLock.current = true;
    setSending(true);
    setError("");
    const submission =
      retry && pending ? pending : { content: content.trim(), client_request_id: crypto.randomUUID() };
    const optimistic: Message = {
      id: `pending:${submission.client_request_id}`,
      role: "user",
      content: submission.content,
      status: "sending",
      created_at: optimisticMessage?.created_at ?? new Date().toISOString(),
    };
    setPending(submission);
    setOptimisticMessage(optimistic);
    try {
      const accepted = await api<Accepted>("/chat/messages", {
        method: "POST",
        body: JSON.stringify(submission),
      });
      setPending(null);
      setOptimisticMessage(null);
      setMessages((previous) =>
        previous.some((m) => m.id === accepted.message_id)
          ? previous
          : [
              ...previous,
              {
                id: accepted.message_id,
                role: "user",
                content: submission.content,
                status: "sent",
                created_at: optimistic.created_at,
              },
            ],
      );
      setStatus(
        activeRun.current
          ? "Correction received. Replanning at the next safe checkpoint; an action already dispatched may still complete."
          : "Working on your request…",
      );
      connect(accepted.run_id);
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status < 500) {
        setPending(null);
        setOptimisticMessage(null);
      } else {
        setOptimisticMessage({ ...optimistic, status: "unconfirmed" });
      }
      report(e);
      return false;
    } finally {
      sendLock.current = false;
      setSending(false);
    }
  }
  async function stop() {
    if (!run || stopping) return;
    setStopping(true);
    setError("");
    try {
      const result = await api<{ status: string; dispatch_committed: boolean }>(
        `/runs/${encodeURIComponent(run)}/stop`,
        { method: "POST" },
      );
      setStatus(
        result.dispatch_committed
          ? "Stop requested. An action already dispatched may still complete; waiting for its outcome."
          : "Stop requested. The assistant will pause at the next safe checkpoint.",
      );
    } catch (e) {
      report(e);
    } finally {
      setStopping(false);
    }
  }
  return {
    messages: visibleMessages,
    receipts,
    loading,
    error,
    status,
    run,
    streamState,
    sending,
    stopping,
    pending,
    submit,
    stop,
    reconnect: () => {
      if (run) connect(run, false);
    },
    reload: async () => {
      setLoading(true);
      try {
        await history();
      } catch (e) {
        report(e);
      } finally {
        setLoading(false);
      }
    },
    loadOlder: cursor ? () => history(cursor).catch(report) : undefined,
  };
}

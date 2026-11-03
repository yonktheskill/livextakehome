"use client";
import { useEffect, useRef, useState } from "react";
import { Button, Textarea } from "@headlessui/react";
import { ArrowDown, ArrowUp, Clock3, KeyRound, LogOut, MessageSquare, RefreshCw, Square, Sun } from "lucide-react";
import { useAuth } from "./auth";
import { api, ApiError, type Connection, type User } from "../lib/api";
import { useConversation } from "../lib/use-conversation";
import { ConnectionDialog } from "./connection-dialog";
import { ChatSidebar } from "./chat-sidebar";
import { ActionReceipt, Welcome } from "./chat-content";
import { SafeMarkdown } from "./markdown";
export function Chat({ user }: { user: User }) {
  const { logout, refresh } = useAuth();
  const chat = useConversation(user.id, refresh);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [dialog, setDialog] = useState(false);
  const [draft, setDraft] = useState("");
  const [accountError, setAccountError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  async function loadConnection() {
    setConnectionError("");
    try {
      setConnection(await api<Connection>("/connection"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) void refresh();
      else setConnectionError(e instanceof Error ? e.message : "Unable to load connection.");
    }
  }
  useEffect(() => {
    let alive = true;
    api<Connection>("/connection")
      .then((value) => {
        if (alive) setConnection(value);
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 401) void refresh();
        else setConnectionError(e instanceof Error ? e.message : "Unable to load connection.");
      });
    return () => {
      alive = false;
    };
  }, [refresh]);
  useEffect(() => {
    if (nearBottom.current) bottom.current?.scrollIntoView({ behavior: "instant", block: "end" });
  }, [chat.messages, chat.status, chat.receipts]);
  async function submit() {
    if (!draft.trim() || chat.pending) return;
    const sent = draft;
    nearBottom.current = true;
    if (await chat.submit(sent)) setDraft((current) => (current === sent ? "" : current));
    input.current?.focus();
  }
  async function signOut() {
    setLoggingOut(true);
    setAccountError("");
    try {
      await logout();
    } catch (e) {
      setAccountError(e instanceof Error ? e.message : "Unable to sign out.");
    } finally {
      setLoggingOut(false);
    }
  }
  return (
    <div className="app-shell">
      <ChatSidebar
        user={user}
        connection={connection}
        loggingOut={loggingOut}
        onConnect={() => setDialog(true)}
        onSignOut={signOut}
      />
      <main className="chat-main">
        <header className="chat-header">
          <div>
            <span className="mobile-brand">daylight.</span>
            <h1>Your conversation</h1>
            <p>One place to keep your day in motion.</p>
          </div>
          <div className="header-actions">
            <span className="timezone">
              <Clock3 size={14} />
              {user.time_zone}
            </span>
            <Button className="connection-pill" onClick={() => setDialog(true)}>
              <span className={`status-dot ${connection?.connected ? "" : "offline"}`} />
              {connection?.connected ? "Cal.com connected" : "Connect Cal.com"}
            </Button>
            <Button
              className="mobile-logout icon-button"
              aria-label="Sign out"
              onClick={() => void signOut()}
              disabled={loggingOut}
            >
              <LogOut size={18} />
            </Button>
          </div>
        </header>
        {(connectionError || accountError) && (
          <div role="alert" className="top-error">
            {connectionError || accountError}
            {connectionError && (
              <Button className="text-button" onClick={() => void loadConnection()}>
                Try again
              </Button>
            )}
          </div>
        )}
        {connection && !connection.connected && (
          <div className="connection-banner">
            <KeyRound size={17} />
            <span>Connect Cal.com to check availability and manage bookings.</span>
            <Button onClick={() => setDialog(true)}>
              Connect <ArrowUp className="rotate-45" size={14} />
            </Button>
          </div>
        )}
        <div
          className="conversation-scroll"
          ref={scroll}
          onScroll={() => {
            const el = scroll.current;
            if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 140;
          }}
        >
          <div className="conversation-content">
            {chat.loading ? (
              <div className="loading-state" role="status">
                <span className="loading-orbit" />
                Opening your conversation…
              </div>
            ) : (
              <>
                {chat.loadOlder && (
                  <Button className="older-button" onClick={() => void chat.loadOlder?.()}>
                    Load earlier messages
                  </Button>
                )}
                {!chat.messages.length && (
                  <Welcome
                    onPrompt={(text) => {
                      setDraft(text);
                      input.current?.focus();
                    }}
                  />
                )}
                {chat.messages
                  .filter((m) => ["user", "assistant"].includes(m.role))
                  .map((message) => (
                    <article
                      key={message.id}
                      className={`message ${message.role === "user" ? "message-user" : "message-assistant"}`}
                    >
                      <div className="message-avatar">
                        {message.role === "user" ? user.email[0].toUpperCase() : <Sun size={19} />}
                      </div>
                      <div className="message-body">
                        <div className="message-label">
                          {message.role === "user" ? "You" : "Daylight"}
                          <span>
                            {new Date(message.created_at).toLocaleTimeString([], {
                              hour: "numeric",
                              minute: "2-digit",
                            })}
                          </span>
                        </div>
                        <SafeMarkdown content={message.content} />
                      </div>
                    </article>
                  ))}
                {chat.receipts.map((receipt) => (
                  <ActionReceipt key={receipt.action_id} receipt={receipt} />
                ))}
              </>
            )}
            {chat.status && (
              <div className="run-status" role="status">
                {chat.run && <span className="pulse-dot" />}
                {chat.status}
              </div>
            )}
            {chat.error && (
              <div className="error-box" role="alert">
                {chat.error}
                {!chat.messages.length && (
                  <Button className="text-button" onClick={() => void chat.reload()}>
                    Reload conversation
                  </Button>
                )}
              </div>
            )}
            {chat.pending && !chat.sending && (
              <div className="recovery-box">
                <p>Delivery wasn’t confirmed. Retry this exact message safely with the same request ID.</p>
                <Button
                  className="text-button"
                  disabled={chat.sending}
                  onClick={async () => {
                    if (await chat.submit("", true)) setDraft("");
                  }}
                >
                  Retry message
                </Button>
              </div>
            )}
            <div ref={bottom} />
          </div>
        </div>
        <footer className="composer-area">
          <div className="composer-wrap">
            {chat.streamState === "reconnecting" || chat.streamState === "paused" ? (
              <div className="reconnect-state" role="status">
                <RefreshCw size={14} />
                <span>
                  {chat.streamState === "reconnecting"
                    ? "Reconnecting to your response…"
                    : "Response connection paused"}
                </span>
                {chat.streamState === "paused" && <Button onClick={chat.reconnect}>Reconnect</Button>}
              </div>
            ) : null}
            {chat.run && (
              <div className="execution-bar">
                <span>You can send a correction while I work.</span>
                <Button className="stop-button" disabled={chat.stopping} onClick={() => void chat.stop()}>
                  <Square size={11} fill="currentColor" />
                  {chat.stopping ? "Stopping…" : "Stop"}
                </Button>
              </div>
            )}
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                void submit();
              }}
            >
              <label className="sr-only" htmlFor="message">
                Message Daylight
              </label>
              <Textarea
                id="message"
                ref={input}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                maxLength={16000}
                placeholder={chat.run ? "A change of plan? Tell me…" : "What would you like to plan?"}
                rows={2}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void submit();
                  }
                }}
              />
              <div className="composer-bottom">
                <span>
                  <MessageSquare size={13} /> {chat.run ? "Corrections welcome" : "Your scheduling assistant"}
                </span>
                <Button
                  aria-label={chat.run ? "Send correction" : "Send message"}
                  className="send-button"
                  type="submit"
                  disabled={!draft.trim() || chat.sending || chat.loading || !!chat.pending}
                >
                  {chat.sending ? <span className="button-loader" /> : <ArrowUp size={19} />}
                </Button>
              </div>
            </form>
            <p className="composer-hint">
              Enter to send <span>·</span> Shift + Enter for a new line <span>·</span> Times shown with their time zone
            </p>
          </div>
        </footer>
        <Button
          className="scroll-bottom"
          aria-label="Scroll to latest message"
          onClick={() => {
            nearBottom.current = true;
            bottom.current?.scrollIntoView({ behavior: "smooth" });
          }}
        >
          <ArrowDown size={17} />
        </Button>
      </main>
      {dialog && (
        <ConnectionDialog
          open={dialog}
          onClose={() => setDialog(false)}
          connection={connection}
          onChange={setConnection}
          onUnauthorized={refresh}
        />
      )}
    </div>
  );
}

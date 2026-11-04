"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Textarea } from "@headlessui/react";
import { ArrowDown, ArrowUp, BookOpen, RefreshCw, Square, Paperclip, X } from "lucide-react";
import { useAuth } from "./auth";
import { api, ApiError, type Attachment, type Connection, type User } from "../lib/api";
import { useConversation } from "../lib/use-conversation";
import { conversationTimeline } from "../lib/conversation-timeline";
import { messageTime } from "../lib/timezone";
import { ConnectionDialog } from "./connection-dialog";
import { ChatHeader } from "./chat-header";
import { ActionReceipt, Welcome } from "./chat-content";
import { SafeMarkdown } from "./markdown";
import { MessageCopy } from "./message-copy";
import { MessageImages } from "./message-images";
import { MAX_IMAGES, prepareImage, uploadImage } from "../lib/image-attachments";
type DraftImage = {
  key: string;
  file: File;
  preview?: string;
  attachment?: Attachment;
  status: "processing" | "uploading" | "ready" | "error";
  error?: string;
};
export const MAX_MESSAGE_CHARACTERS = 6000;

export function Chat({ user }: { user: User }) {
  const { logout, refresh, refreshProfile, updateTimeZone } = useAuth();
  const chat = useConversation(user.id, refresh);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [connectionError, setConnectionError] = useState("");
  const [dialog, setDialog] = useState(false);
  const [draft, setDraft] = useState("");
  const [images, setImages] = useState<DraftImage[]>([]);
  const imagesRef = useRef<DraftImage[]>([]);
  const imageUrls = useRef(new Set<string>());
  const fileInput = useRef<HTMLInputElement>(null);
  const [failedCaption, setFailedCaption] = useState("");
  const [imageError, setImageError] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const urls = imageUrls.current;
    return () => {
      alive.current = false;
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);
  function changeImages(update: (current: DraftImage[]) => DraftImage[]) {
    const next = update(imagesRef.current);
    imagesRef.current = next;
    if (alive.current) setImages(next);
  }
  async function processImage(item: DraftImage) {
    if (item.preview) {
      URL.revokeObjectURL(item.preview);
      imageUrls.current.delete(item.preview);
    }
    changeImages((current) =>
      current.map((image) =>
        image.key === item.key ? { ...image, status: "processing", error: undefined } : image,
      ),
    );
    try {
      const blob = await prepareImage(item.file);
      if (!alive.current || !imagesRef.current.some((image) => image.key === item.key)) return;
      const preview = URL.createObjectURL(blob);
      imageUrls.current.add(preview);
      changeImages((current) =>
        current.map((image) => (image.key === item.key ? { ...image, preview, status: "uploading" } : image)),
      );
      const attachment = await uploadImage(blob);
      changeImages((current) =>
        current.map((image) => (image.key === item.key ? { ...image, attachment, status: "ready" } : image)),
      );
    } catch (error) {
      changeImages((current) =>
        current.map((image) =>
          image.key === item.key
            ? {
                ...image,
                status: "error",
                error: error instanceof Error ? error.message : "Unable to attach image. Try again.",
              }
            : image,
        ),
      );
    }
  }
  function addImages(files: File[]) {
    setImageError("");
    if (imagesRef.current.length + files.length > MAX_IMAGES) {
      setImageError("Attach up to four images per message.");
      return;
    }
    const added: DraftImage[] = files.map((file) => ({
      key: crypto.randomUUID(),
      file,
      status: "processing",
    }));
    changeImages((current) => [...current, ...added]);
    added.forEach((item) => void processImage(item));
  }
  function removeImages(keys: Set<string>) {
    changeImages((current) =>
      current.filter((image) => {
        if (!keys.has(image.key)) return true;
        if (image.preview) {
          URL.revokeObjectURL(image.preview);
          imageUrls.current.delete(image.preview);
        }
        return false;
      }),
    );
  }
  const imagesBusy = images.some((image) => image.status !== "ready");
  const draftLength = Array.from(draft).length;
  const tooLong = draftLength > MAX_MESSAGE_CHARACTERS;
  const [accountError, setAccountError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const scrollToBottom = useCallback((behavior: ScrollBehavior) => {
    const el = scroll.current;
    // Keep ancestor containers (including the overflow-hidden app shell) in place.
    el?.scrollTo({ top: Math.max(0, el.scrollHeight - el.clientHeight), behavior });
  }, []);
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
    if (nearBottom.current) scrollToBottom("instant");
  }, [chat.messages, chat.status, chat.receipts, scrollToBottom]);
  async function submit() {
    if (
      tooLong ||
      (!draft.trim() && !images.length) ||
      imagesBusy ||
      chat.pending ||
      chat.sending ||
      chat.loading
    )
      return;
    const sent = draft;
    const sentImages = images;
    nearBottom.current = true;
    setDraft("");
    if (
      !(await chat.submit(
        sent,
        false,
        sentImages.map((image) => image.attachment!),
      ))
    ) {
      setDraft((current) => current || sent);
      setFailedCaption(sent);
    } else {
      setFailedCaption("");
      removeImages(new Set(sentImages.map((image) => image.key)));
    }
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
      <ChatHeader
        user={user}
        connection={connection}
        loggingOut={loggingOut}
        onConnect={() => setDialog(true)}
        onSignOut={signOut}
        onTimeZoneChange={updateTimeZone}
        onUnauthorized={refresh}
      />
      <main className="chat-main">
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
            <span>Connect Cal.com to manage bookings.</span>
            <Button onClick={() => setDialog(true)}>Connect</Button>
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
                Loading…
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
                {conversationTimeline(chat.messages, chat.receipts).map((item) => {
                  if (item.type === "receipt")
                    return (
                      <ActionReceipt
                        key={`action:${item.receipt.action_id}`}
                        receipt={item.receipt}
                        timeZone={user.time_zone}
                      />
                    );
                  const message = item.message;
                  return (
                    <article
                      key={message.id}
                      className={`message ${message.role === "user" ? "message-user" : "message-assistant"}`}
                    >
                      <div className="message-avatar">
                        {message.role === "user" ? (
                          user.email[0].toUpperCase()
                        ) : (
                          <BookOpen size={19} aria-hidden="true" />
                        )}
                      </div>
                      <div className="message-body">
                        <div className="message-label">
                          {message.role === "user" ? "You" : "HelpBook"}
                          <span>{messageTime(message.created_at, user.time_zone)}</span>
                          {message.status === "sending" && <span>Sending…</span>}
                          {message.status === "unconfirmed" && <span>Delivery unconfirmed</span>}
                        </div>
                        <MessageImages
                          attachments={message.attachments}
                          onLoad={() => {
                            if (nearBottom.current) scrollToBottom("instant");
                          }}
                        />
                        <SafeMarkdown content={message.content} />
                        <MessageCopy content={message.content} />
                      </div>
                    </article>
                  );
                })}
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
                <p>Message delivery wasn’t confirmed.</p>
                <Button
                  className="text-button"
                  disabled={chat.sending}
                  onClick={async () => {
                    const content = chat.pending?.content;
                    const attachmentIds = new Set(chat.pending?.attachment_ids ?? []);
                    if (await chat.submit("", true)) {
                      setDraft((current) => (current.trim() === content ? "" : current));
                      removeImages(
                        new Set(
                          imagesRef.current
                            .filter((image) => attachmentIds.has(image.attachment?.id ?? ""))
                            .map((image) => image.key),
                        ),
                      );
                    }
                  }}
                >
                  Retry message
                </Button>
              </div>
            )}
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
            <form
              className="composer"
              onDragOver={(e) => {
                if (e.dataTransfer.types.includes("Files")) e.preventDefault();
              }}
              onDrop={(e) => {
                e.preventDefault();
                addImages(Array.from(e.dataTransfer.files));
              }}
              onPaste={(e) => {
                const files = Array.from(e.clipboardData.items)
                  .filter((item) => item.kind === "file")
                  .map((item) => item.getAsFile())
                  .filter((file): file is File => !!file);
                if (files.length) {
                  e.preventDefault();
                  addImages(files);
                }
              }}
              onSubmit={(e) => {
                e.preventDefault();
                void submit();
              }}
            >
              <input
                ref={fileInput}
                type="file"
                className="sr-only"
                accept="image/jpeg,image/png,image/webp"
                multiple
                aria-label="Choose image attachments"
                onChange={(e) => {
                  addImages(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
              {!!images.length && (
                <ul className="draft-images" aria-label="Image attachments">
                  {images.map((image, i) => (
                    <li key={image.key}>
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      {image.preview && <img src={image.preview} alt={`Image ${i + 1} preview`} />}
                      <span>{image.file.name}</span>
                      <span role="status">
                        {image.status === "processing"
                          ? "Processing image…"
                          : image.status === "uploading"
                            ? "Uploading image…"
                            : image.status === "ready"
                              ? "Ready"
                              : "Upload failed"}
                      </span>
                      {image.error && <span role="alert">{image.error}</span>}
                      {image.status === "error" && (
                        <Button type="button" onClick={() => void processImage(image)}>
                          Retry upload
                        </Button>
                      )}
                      <Button
                        type="button"
                        aria-label={`Remove image ${i + 1}`}
                        onClick={() => removeImages(new Set([image.key]))}
                      >
                        <X size={16} />
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
              {imageError && <p role="alert">{imageError}</p>}
              {failedCaption && draft !== failedCaption && (
                <div className="recovery-box">
                  <p>Previous caption: {failedCaption}</p>
                  <Button
                    type="button"
                    onClick={() => {
                      setDraft((current) => (current ? `${failedCaption}\n${current}` : failedCaption));
                      setFailedCaption("");
                    }}
                  >
                    Restore previous caption
                  </Button>
                </div>
              )}
              <label className="sr-only" htmlFor="message">
                Message HelpBook
              </label>
              <Textarea
                id="message"
                ref={input}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                aria-describedby="message-length"
                invalid={tooLong}
                placeholder="Message HelpBook…"
                rows={2}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    void submit();
                  }
                }}
              />
              <div className="composer-bottom">
                <Button
                  type="button"
                  className="attach-button"
                  aria-label="Attach images"
                  title="Attach images (or paste/drop a screenshot)"
                  disabled={images.length >= MAX_IMAGES}
                  onClick={() => fileInput.current?.click()}
                >
                  <Paperclip size={20} />
                </Button>
                {chat.run ? (
                  <Button
                    aria-label={chat.stopping ? "Stopping response" : "Stop response"}
                    title={chat.stopping ? "Stopping…" : "Stop response"}
                    className="send-button stop-button"
                    type="button"
                    disabled={chat.stopping}
                    onClick={() => void chat.stop()}
                  >
                    {chat.stopping ? (
                      <span className="button-loader" />
                    ) : (
                      <Square size={17} fill="currentColor" />
                    )}
                  </Button>
                ) : null}
                {(!chat.run || !!draft.trim() || !!images.length) && (
                  <Button
                    aria-label="Send message"
                    className="send-button"
                    type="submit"
                    disabled={
                      tooLong ||
                      (!draft.trim() && !images.length) ||
                      imagesBusy ||
                      chat.sending ||
                      chat.loading ||
                      !!chat.pending
                    }
                  >
                    {chat.sending ? <span className="button-loader" /> : <ArrowUp size={22} />}
                  </Button>
                )}
              </div>
            </form>
            <p id="message-length" role={tooLong ? "alert" : undefined}>
              {tooLong
                ? "Message is too long. Shorten it to 6,000 characters before sending."
                : `${draftLength.toLocaleString("en-US")} / 6,000 characters`}
            </p>
          </div>
          <Button
            className="scroll-bottom"
            aria-label="Scroll to latest message"
            onClick={() => {
              nearBottom.current = true;
              scrollToBottom("smooth");
            }}
          >
            <ArrowDown size={17} />
          </Button>
        </footer>
      </main>
      {dialog && (
        <ConnectionDialog
          open={dialog}
          onClose={() => setDialog(false)}
          connection={connection}
          onChange={(value) => {
            setConnection(value);
            setAccountError("");
            void refreshProfile().catch((e) =>
              setAccountError(e instanceof Error ? e.message : "Unable to refresh your profile."),
            );
          }}
          onUnauthorized={refresh}
        />
      )}
    </div>
  );
}

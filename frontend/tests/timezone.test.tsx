import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "../components/auth";
import { Chat } from "../components/chat";

const user = { id: 1, email: "owner@example.test", time_zone: "UTC" };
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function SignedInChat() {
  const { user } = useAuth();
  return user ? <Chat user={user} /> : null;
}

describe("timezone dropdown", () => {
  const originalScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  const originalIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  beforeEach(() => {
    sessionStorage.clear();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
    vi.stubGlobal("PointerEvent", MouseEvent);
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.spyOn(Intl, "supportedValuesOf").mockReturnValue([
      "America/New_York",
      "Asia/Kolkata",
      "Asia/Singapore",
    ]);
  });
  afterEach(() => {
    for (const [name, descriptor] of [
      ["scrollTo", originalScroll],
      ["scrollIntoView", originalIntoView],
    ] as const) {
      if (descriptor) Object.defineProperty(HTMLElement.prototype, name, descriptor);
      else Reflect.deleteProperty(HTMLElement.prototype, name);
    }
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function start(
    save: (init: RequestInit) => Promise<Response> = async () => json(user),
    currentUser = user,
  ) {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/auth/me") {
        if (init?.method === "PATCH") return save(init);
        return json(currentUser);
      }
      if (url.endsWith("/connection")) return json({ connected: true, label: "Connected" });
      if (url.endsWith("/history"))
        return json({
          messages: [
            {
              id: "message-1",
              role: "user",
              content: "Existing message",
              created_at: "2026-01-01T12:00:00Z",
              status: "completed",
            },
          ],
          next_cursor: null,
        });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetcher);
    render(
      <AuthProvider>
        <SignedInChat />
      </AuthProvider>,
    );
    await screen.findByText("Existing message");
    return fetcher;
  }

  async function open() {
    const trigger = screen.getByRole("button", { name: "Change timezone, currently UTC" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const search = await screen.findByRole("textbox", { name: "Search timezones" });
    await waitFor(() => expect(search).toHaveFocus());
    return { trigger, search };
  }

  it("searches, navigates both directions, and saves with Enter without losing the chat draft", async () => {
    let resolve!: (response: Response) => void;
    const saving = new Promise<Response>((done) => {
      resolve = done;
    });
    const fetcher = await start(() => saving);
    const composer = screen.getByRole("textbox", { name: "Message HelpBook" });
    fireEvent.change(composer, { target: { value: "Keep my draft" } });
    const { trigger, search } = await open();
    expect(document.querySelector("select")).toBeNull();
    expect(screen.getByRole("menuitemradio", { name: "UTC" })).toHaveAttribute("aria-checked", "true");
    fireEvent.change(search, { target: { value: "Asia" } });
    fireEvent.keyDown(search, { key: "ArrowDown" });
    const kolkata = screen.getByRole("menuitemradio", { name: "Asia/Kolkata" });
    const singapore = screen.getByRole("menuitemradio", { name: "Asia/Singapore" });
    expect(kolkata).toHaveFocus();
    fireEvent.keyDown(kolkata, { key: "ArrowDown" });
    await waitFor(() => expect(singapore).toHaveFocus());
    fireEvent.keyDown(singapore, { key: "ArrowUp" });
    await waitFor(() => expect(kolkata).toHaveFocus());
    fireEvent.keyDown(kolkata, { key: "ArrowDown" });
    await waitFor(() => expect(singapore).toHaveFocus());
    fireEvent.keyDown(singapore, { key: "Enter" });
    expect(await screen.findByRole("status")).toHaveTextContent("Saving timezone…");
    expect(trigger).toHaveTextContent("UTC");
    expect(fetcher).toHaveBeenCalledWith(
      "/api/auth/me",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ time_zone: "Asia/Singapore" }) }),
    );
    await act(async () => resolve(json({ ...user, time_zone: "Asia/Singapore" })));
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Change timezone, currently Asia/Singapore" })).toHaveFocus();
    expect(composer).toHaveValue("Keep my draft");
    expect(
      screen.getByText(
        new Date("2026-01-01T12:00:00Z").toLocaleTimeString([], {
          hour: "numeric",
          minute: "2-digit",
          timeZone: "Asia/Singapore",
        }),
      ),
    ).toBeInTheDocument();
    expect(fetcher.mock.calls.filter(([url]) => url.endsWith("/history"))).toHaveLength(1);
  });

  it("opens with the mouse and allows a failed save to be retried", async () => {
    const save = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: "Unable to save. Try again." }, 503))
      .mockResolvedValueOnce(json({ ...user, time_zone: "Asia/Singapore" }));
    await start(save);
    const trigger = screen.getByRole("button", { name: "Change timezone, currently UTC" });
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    const singapore = await screen.findByRole("menuitemradio", { name: "Asia/Singapore" });
    fireEvent.click(singapore);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to save. Try again.");
    expect(trigger).toHaveTextContent("UTC");
    expect(singapore).not.toHaveAttribute("data-disabled");
    fireEvent.click(singapore);
    await screen.findByRole("button", { name: "Change timezone, currently Asia/Singapore" });
    expect(save).toHaveBeenCalledTimes(2);
  });

  it("handles no search results and dismisses with Escape without saving", async () => {
    const save = vi.fn();
    await start(save);
    const { trigger, search } = await open();
    fireEvent.change(search, { target: { value: "nonexistent city" } });
    expect(screen.queryAllByRole("menuitemradio")).toHaveLength(0);
    expect(screen.getByText("No timezones found.")).toBeInTheDocument();
    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(search).toHaveFocus();
    fireEvent.keyDown(search, { key: "Enter" });
    fireEvent.keyDown(search, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(save).not.toHaveBeenCalled();
  });

  it("still allows correction when the imported timezone is invalid", async () => {
    const save = vi.fn(async () => json({ ...user, time_zone: "Asia/Singapore" }));
    await start(save, { ...user, time_zone: "Invalid/Timezone" });
    const trigger = screen.getByRole("button", { name: "Change timezone, currently Invalid/Timezone" });
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "Asia/Singapore" }));
    expect(
      await screen.findByRole("button", { name: "Change timezone, currently Asia/Singapore" }),
    ).toBeVisible();
  });
});

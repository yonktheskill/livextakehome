import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "../components/auth";
import { Chat } from "../components/chat";

function Conversation() {
  const { user, loading } = useAuth();
  return loading ? <p>Checking</p> : user ? <Chat key={user.id} user={user} /> : <p>Anonymous</p>;
}
beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it("refreshes the connection timezone without remounting chat or losing the draft", async () => {
  let profileReads = 0;
  const fetcher = vi.fn(async (url: string) => {
    const value =
      url === "/api/auth/me"
        ? { id: 1, email: "owner@example.test", time_zone: ++profileReads > 1 ? "Asia/Singapore" : "UTC" }
        : url.includes("history")
          ? { messages: [], next_cursor: null }
          : { connected: true, label: "Connected" };
    return new Response(JSON.stringify(value));
  });
  vi.stubGlobal("fetch", fetcher);
  render(
    <AuthProvider>
      <Conversation />
    </AuthProvider>,
  );
  const input = await screen.findByRole("textbox", { name: "Message HelpBook" });
  fireEvent.change(input, { target: { value: "Keep my draft" } });
  fireEvent.click(screen.getByRole("button", { name: /User menu/ }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /Cal.com connection/ }));
  const key = await screen.findByLabelText("Replace API key");
  fireEvent.change(key, { target: { value: "offline-test-key" } });
  fireEvent.submit(key.closest("form")!);
  await waitFor(() => expect(profileReads).toBe(2));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message HelpBook" })).toBe(input));
  expect(input).toHaveValue("Keep my draft");
  expect(fetcher.mock.calls.filter(([url]) => url.includes("history"))).toHaveLength(1);
  expect(screen.getByRole("button", { name: /Asia\/Singapore/ })).toBeInTheDocument();
});

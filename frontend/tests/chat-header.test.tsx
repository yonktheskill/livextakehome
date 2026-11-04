import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";

const { logout, refresh } = vi.hoisted(() => ({ logout: vi.fn(), refresh: vi.fn() }));
vi.mock("../components/auth", () => ({
  useAuth: () => ({ logout, refresh, refreshProfile: async () => {} }),
}));
vi.mock("../lib/use-conversation", () => ({
  useConversation: () => ({ messages: [], receipts: [], loading: false, status: "", streamState: "idle" }),
}));

const user = { id: 1, email: "owner@example.test", time_zone: "Asia/Singapore" };
const connected = { connected: true, label: "Cal.com • 3d5168b1" };
const json = (value: unknown) => new Response(JSON.stringify(value));
const userMenu = () => screen.getByRole("button", { name: `User menu for ${user.email}` });

describe("topbar account controls", () => {
  const originalScrollTo = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  beforeEach(() => {
    logout.mockReset().mockResolvedValue(undefined);
    refresh.mockClear();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    // JSDOM has no layout observers; Headless UI uses this to position the menu.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => json(connected)),
    );
  });
  afterEach(() => {
    cleanup();
    if (originalScrollTo) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScrollTo);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
    vi.unstubAllGlobals();
  });

  async function openMenu() {
    fireEvent.click(userMenu());
    return screen.findByRole("menu");
  }

  it("places the brand, timezone, and two-option user menu in the topbar", async () => {
    render(<Chat user={user} />);
    const header = within(screen.getByRole("banner"));
    expect(header.getByRole("link", { name: "HelpBook" })).toHaveAttribute("href", "/");
    expect(header.getByText("UTC+08:00")).toBeInTheDocument();
    expect(header.getByRole("button", { name: `User menu for ${user.email}` })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.queryByText("Chat")).not.toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    const menu = within(await openMenu());
    expect(menu.getAllByRole("menuitem")).toHaveLength(2);
    expect(await menu.findByRole("menuitem", { name: "Cal.com connection Connected" })).toBeInTheDocument();
    expect(menu.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
    fireEvent.click(menu.getByRole("menuitem", { name: /Cal.com connection/ }));

    const dialog = within(await screen.findByRole("dialog", { name: "Your Cal.com connection" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(
      dialog.getByText("Connect your account so HelpBook can check availability and manage your bookings."),
    ).toBeInTheDocument();
    expect(dialog.getByText(connected.label)).toBeInTheDocument();
    expect(dialog.getByLabelText("Replace API key")).toHaveAttribute("type", "password");
    fireEvent.click(dialog.getByRole("button", { name: "Close connection settings" }));
    await waitFor(() => expect(userMenu()).toHaveFocus());
  });

  it("opens connection settings when disconnected and reflects a newly connected account", async () => {
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) =>
      json(init?.method === "PUT" ? connected : { connected: false, label: null }),
    );
    vi.stubGlobal("fetch", fetcher);
    render(<Chat user={user} />);
    expect(await screen.findByText("Connect Cal.com to manage bookings.")).toBeInTheDocument();
    const menu = within(await openMenu());
    fireEvent.click(menu.getByRole("menuitem", { name: "Cal.com connection Not connected" }));
    const dialog = within(await screen.findByRole("dialog"));
    fireEvent.change(dialog.getByLabelText("Cal.com API key"), { target: { value: "test-cal-key" } });
    fireEvent.click(dialog.getByRole("button", { name: "Connect Cal.com" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(fetcher).toHaveBeenCalledWith(
      "/api/connection",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ api_key: "test-cal-key" }) }),
    );
    expect(screen.queryByText("Connect Cal.com to manage bookings.")).not.toBeInTheDocument();
    fireEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Cal.com connection Connected" }));
    expect(await screen.findByLabelText("Replace API key")).toBeInTheDocument();
    expect(screen.getByText(connected.label)).toBeInTheDocument();
  });

  it("supports keyboard navigation and Escape on the user menu", async () => {
    render(<Chat user={user} />);
    userMenu().focus();
    fireEvent.keyDown(userMenu(), { key: "ArrowDown" });
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitem");
    await waitFor(() => expect(menu).toHaveAttribute("aria-activedescendant", items[0].id));
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(menu).toHaveAttribute("aria-activedescendant", items[1].id);
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(userMenu()).toHaveFocus();
    expect(logout).not.toHaveBeenCalled();
  });

  it("signs out through the menu, prevents duplicate requests, and allows retry after failure", async () => {
    let rejectLogout!: (error: Error) => void;
    logout.mockReturnValueOnce(
      new Promise<void>((_resolve, reject) => {
        rejectLogout = reject;
      }),
    );
    render(<Chat user={user} />);
    fireEvent.click(within(await openMenu()).getByRole("menuitem", { name: "Sign out" }));
    expect(logout).toHaveBeenCalledTimes(1);
    const pending = within(await openMenu()).getByRole("menuitem", { name: "Signing out…" });
    expect(pending).toBeDisabled();
    fireEvent.click(pending);
    expect(logout).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    await act(async () => rejectLogout(new Error("Unable to sign out. Please try again.")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to sign out. Please try again.");
    const retry = within(await openMenu()).getByRole("menuitem", { name: "Sign out" });
    expect(retry).toBeEnabled();
    await act(async () => fireEvent.click(retry));
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";

const { conversation, refresh } = vi.hoisted(() => ({
  refresh: () => {},
  conversation: {
    messages: [],
    receipts: [],
    loading: false,
    status: "",
    streamState: "idle",
  },
}));
vi.mock("../components/auth", () => ({ useAuth: () => ({ refresh, logout: vi.fn() }) }));
vi.mock("../lib/use-conversation", () => ({ useConversation: () => conversation }));
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  api: async () => ({ connected: true }),
}));

const user = { id: 1, email: "owner@example.test", time_zone: "UTC" };

describe("chat scrolling", () => {
  const originalScrollTo = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTo");
  const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollIntoView");
  beforeEach(() => {
    conversation.status = "";
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: vi.fn() });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    if (originalScrollTo) Object.defineProperty(HTMLElement.prototype, "scrollTo", originalScrollTo);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollTo");
    if (originalScrollIntoView)
      Object.defineProperty(HTMLElement.prototype, "scrollIntoView", originalScrollIntoView);
    else Reflect.deleteProperty(HTMLElement.prototype, "scrollIntoView");
  });

  it.each([
    { scrollHeight: 2000, clientHeight: 600, bottom: 1400 },
    { scrollHeight: 300, clientHeight: 600, bottom: 0 },
  ])("scrolls only the conversation to $bottom", (dimensions) => {
    const { container } = render(<Chat user={user} />);
    const el = container.querySelector<HTMLElement>(".conversation-scroll")!;
    Object.defineProperties(el, {
      scrollHeight: { value: dimensions.scrollHeight },
      clientHeight: { value: dimensions.clientHeight },
    });
    const scrollTo = vi.spyOn(el, "scrollTo");
    scrollTo.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Scroll to latest message" }));

    expect(scrollTo).toHaveBeenCalledExactlyOnceWith({ top: dimensions.bottom, behavior: "smooth" });
    expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
  });

  it("follows updates locally, respects scrolling up, and resumes after clicking latest", () => {
    const { container, rerender } = render(<Chat user={user} />);
    const el = container.querySelector<HTMLElement>(".conversation-scroll")!;
    Object.defineProperties(el, {
      scrollHeight: { value: 2000 },
      clientHeight: { value: 600 },
    });
    const scrollTo = vi.spyOn(el, "scrollTo");
    scrollTo.mockClear();
    conversation.status = "Working";
    rerender(<Chat user={user} />);
    expect(scrollTo).toHaveBeenCalledExactlyOnceWith({ top: 1400, behavior: "instant" });

    fireEvent.scroll(el, { target: { scrollTop: 100 } });
    scrollTo.mockClear();
    conversation.status = "Still working";
    rerender(<Chat user={user} />);
    expect(scrollTo).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Scroll to latest message" }));
    scrollTo.mockClear();
    conversation.status = "Done";
    rerender(<Chat user={user} />);
    expect(scrollTo).toHaveBeenCalledExactlyOnceWith({ top: 1400, behavior: "instant" });
    expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
  });
});

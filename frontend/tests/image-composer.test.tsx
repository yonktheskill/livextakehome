import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { Chat } from "../components/chat";
import { prepareImage, uploadImage } from "../lib/image-attachments";
const authorized = () => {};
vi.mock("../components/auth", () => ({ useAuth: () => ({ logout: vi.fn(), refresh: authorized }) }));
vi.mock("../lib/image-attachments", () => ({ MAX_IMAGES: 4, prepareImage: vi.fn(), uploadImage: vi.fn() }));
const json = (value: unknown) => new Response(JSON.stringify(value));
beforeEach(() => {
  sessionStorage.clear();
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: vi.fn() });
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:preview") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  vi.mocked(prepareImage).mockResolvedValue(new Blob(["processed"], { type: "image/png" }));
  vi.mocked(uploadImage).mockResolvedValue({ id: "uploaded-image", width: 1000, height: 800 });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("uploads before image-only Send, preserves attachments and a concurrent caption on failure, retries IDs", async () => {
  let reject!: (reason: Error) => void;
  let sends = 0;
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/history")) return json({ messages: [], next_cursor: null });
    if (url.endsWith("/connection")) return json({ connected: true, label: "connected" });
    if (url.endsWith("/messages")) {
      sends++;
      if (sends === 1)
        return new Promise<Response>((_, fail) => {
          reject = fail;
        });
      return json({ run_id: "run", message_id: "message", revision: 1 });
    }
    return new Response(new ReadableStream());
  });
  vi.stubGlobal("fetch", fetcher);
  const { unmount } = render(<Chat user={{ id: 1, email: "owner@example.test", time_zone: "UTC" }} />);
  await screen.findByRole("heading", { name: "What’s on your calendar?" });
  fireEvent.change(screen.getByLabelText("Choose image attachments"), {
    target: { files: [new File(["original"], "meeting.png", { type: "image/png" })] },
  });
  await screen.findByText("Ready");
  fireEvent.click(screen.getByRole("button", { name: "Send message" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Message HelpBook" }), {
    target: { value: "New caption while sending" },
  });
  await act(async () => reject(new Error("offline")));
  expect(screen.getByRole("textbox")).toHaveValue("New caption while sending");
  expect(screen.getByAltText("Image 1 preview")).toBeInTheDocument();
  const saved = JSON.parse(sessionStorage.getItem("daylight-pending:1")!);
  expect(saved.attachment_ids).toEqual(["uploaded-image"]);
  fireEvent.click(screen.getByRole("button", { name: "Retry message" }));
  await waitFor(() => expect(screen.queryByAltText("Image 1 preview")).not.toBeInTheDocument());
  expect(screen.getByRole("textbox")).toHaveValue("New caption while sending");
  expect(uploadImage).toHaveBeenCalledTimes(1);
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview");
  unmount();
});

it("shows upload errors without losing text and permits removing a failed image", async () => {
  vi.mocked(uploadImage).mockRejectedValueOnce(new Error("Upload unavailable"));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.endsWith("/history")
        ? json({ messages: [], next_cursor: null })
        : json({ connected: true, label: "connected" }),
    ),
  );
  render(<Chat user={{ id: 2, email: "owner@example.test", time_zone: "UTC" }} />);
  await screen.findByRole("heading", { name: "What’s on your calendar?" });
  const input = screen.getByRole("textbox");
  fireEvent.change(input, { target: { value: "Caption" } });
  fireEvent.paste(input, {
    clipboardData: {
      items: [{ kind: "file", getAsFile: () => new File(["original"], "paste.png", { type: "image/png" }) }],
    },
  });
  await screen.findByText("Upload unavailable");
  expect(input).toHaveValue("Caption");
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Remove image 1" }));
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});

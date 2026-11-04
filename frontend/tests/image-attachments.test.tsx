import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { prepareImage } from "../lib/image-attachments";
import { useConversation } from "../lib/use-conversation";
import { MessageImages } from "../components/message-images";

const authorized = () => {};
const json = (value: unknown) => new Response(JSON.stringify(value));
const file = (type: string, bytes = "static") =>
  ({ type, size: bytes.length, arrayBuffer: async () => new TextEncoder().encode(bytes).buffer }) as File;

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
beforeEach(() => sessionStorage.clear());

describe("image processing", () => {
  it("rejects unsupported, oversized and animated files before browser decompression", async () => {
    const decode = vi.fn();
    vi.stubGlobal("createImageBitmap", decode);
    await expect(prepareImage(file("image/gif"))).rejects.toThrow("static JPEG");
    await expect(prepareImage({ type: "image/png", size: 10 * 1024 * 1024 + 1 } as File)).rejects.toThrow(
      "10 MiB",
    );
    await expect(prepareImage(file("image/png", "12345678\x00\x00\x00\x08acTLanimation"))).rejects.toThrow(
      "Animated",
    );
    await expect(prepareImage(file("image/webp", "RIFF1234WEBPANIM\x00\x00\x00\x00"))).rejects.toThrow(
      "Animated",
    );
    expect(decode).not.toHaveBeenCalled();
  });
  it("asks to crop long screenshots instead of silently making their text tiny", async () => {
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn().mockResolvedValue({ width: 1000, height: 12000, close }));
    await expect(prepareImage(file("image/png"))).rejects.toThrow("Crop or split");
    expect(close).toHaveBeenCalled();
  });
  it("normalizes images with orientation and keeps the processed byte limit", async () => {
    const close = vi.fn();
    const bitmap = vi.fn().mockResolvedValue({ width: 1200, height: 800, close });
    vi.stubGlobal("createImageBitmap", bitmap);
    const draw = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillRect: vi.fn(),
      drawImage: draw,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback, mime) =>
      callback(new Blob(["normalized"], { type: mime })),
    );
    const result = await prepareImage(file("image/png"));
    expect(result.type).toBe("image/png");
    expect(bitmap).toHaveBeenCalledWith(expect.anything(), { imageOrientation: "from-image" });
    expect(draw).toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
  });
});

it("reloads and retries an image-only submission with exactly the same UUID and IDs, never image bytes", async () => {
  const submissions: string[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("history")) return json({ messages: [], next_cursor: null });
    if (url.endsWith("/messages")) {
      submissions.push(init!.body as string);
      throw new TypeError("offline");
    }
    return new Response(new ReadableStream());
  });
  vi.stubGlobal("fetch", fetcher);
  const first = renderHook(() => useConversation(1, authorized));
  await waitFor(() => expect(first.result.current.loading).toBe(false));
  await act(async () => {
    await first.result.current.submit("", false, [{ id: "opaque-image", width: 800, height: 600 }]);
  });
  const stored = sessionStorage.getItem("daylight-pending:1")!;
  expect(stored).toContain("opaque-image");
  expect(stored).not.toContain("base64");
  expect(stored).not.toContain("blob:");
  first.unmount();
  const second = renderHook(() => useConversation(1, authorized));
  await waitFor(() => expect(second.result.current.pending?.attachment_ids).toEqual(["opaque-image"]));
  expect(second.result.current.messages[0].attachments).toEqual([{ id: "opaque-image" }]);
  await act(async () => {
    await second.result.current.submit("", true);
  });
  expect(submissions).toHaveLength(2);
  expect(submissions[1]).toBe(submissions[0]);
  expect(JSON.parse(submissions[0])).toEqual({
    content: "",
    client_request_id: expect.any(String),
    attachment_ids: ["opaque-image"],
  });
});

it("renders private attachment sources with stable dimensions and an accessible full view", () => {
  render(<MessageImages attachments={[{ id: "private-id", width: 1200, height: 800 }]} />);
  const image = screen.getByAltText("Attached image 1");
  expect(image).toHaveAttribute("src", "/api/chat/attachments/private-id");
  expect(image).toHaveAttribute("width", "1200");
  fireEvent.click(screen.getByRole("button", { name: "View attached image 1" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.getByAltText("Attached image, full view")).toHaveAttribute(
    "src",
    "/api/chat/attachments/private-id",
  );
});

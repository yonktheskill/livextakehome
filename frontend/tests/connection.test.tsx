import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ConnectionDialog } from "../components/connection-dialog";
describe("Cal.com connection controls", () => {
  it("submits a key only to the backend and displays masked connection status", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ connected: true, label: "••••1234" })));
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn(),
      close = vi.fn(),
      storage = vi.spyOn(Storage.prototype, "setItem");
    render(
      <ConnectionDialog open onClose={close} connection={null} onChange={changed} onUnauthorized={vi.fn()} />,
    );
    const input = screen.getByLabelText("Cal.com API key");
    expect(input).toHaveAttribute("maxlength", "1024");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "mock-offline-key" } });
    fireEvent.click(screen.getByRole("button", { name: "Show Cal.com API key" }));
    expect(input).toHaveAttribute("type", "text");
    expect(input).toHaveValue("mock-offline-key");
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Hide Cal.com API key" }));
    expect(input).toHaveAttribute("type", "password");
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(changed).toHaveBeenCalledWith({ connected: true, label: "••••1234" }));
    expect(fetcher.mock.calls[0][0]).toBe("/api/connection");
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ api_key: "mock-offline-key" }),
    });
    expect(storage).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalled();
    storage.mockRestore();
  });
  it("disconnects the integration only after explicit confirmation", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn();
    render(
      <ConnectionDialog
        open
        onClose={vi.fn()}
        connection={{ connected: true, label: "••••1234" }}
        onChange={changed}
        onUnauthorized={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Disconnect account" }));
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(changed).toHaveBeenCalledWith({ connected: false, label: null }));
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
  });
});

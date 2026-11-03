import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "../components/auth";
import { AuthForm } from "../components/auth-form";
const replace = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
function Session() {
  const { user, loading, error, logout } = useAuth();
  return (
    <>
      <span>{loading ? "Checking" : user?.email || "Anonymous"}</span>
      {error && <p role="alert">{error}</p>}
      <button onClick={() => void logout()}>Log out</button>
    </>
  );
}
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
describe("cookie auth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    replace.mockClear();
  });
  it("loads the current session and clears it only after logout succeeds", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(json({ id: 1, email: "owner@example.test", time_zone: "UTC" }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <AuthProvider>
        <Session />
      </AuthProvider>,
    );
    expect(await screen.findByText("owner@example.test")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Log out"));
    expect(await screen.findByText("Anonymous")).toBeInTheDocument();
    expect(fetcher.mock.calls[1][1]).toMatchObject({ method: "POST", credentials: "same-origin" });
  });
  it("registers and signs in without storing credentials", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: "Not signed in" }, 401))
      .mockResolvedValueOnce(json({ id: 2, email: "new@example.test", time_zone: "UTC" }));
    vi.stubGlobal("fetch", fetcher);
    render(
      <AuthProvider>
        <AuthForm mode="register" />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "new@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "example-passphrase" } });
    fireEvent.submit(screen.getByRole("button", { name: "Create account" }).closest("form")!);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(fetcher.mock.calls[1][0]).toBe("/api/auth/register");
    expect(storage).not.toHaveBeenCalled();
  });
  it("surfaces a failed session check rather than treating it as an expired session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ detail: "Service unavailable" }, 503)));
    render(
      <AuthProvider>
        <Session />
      </AuthProvider>,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
  });
});

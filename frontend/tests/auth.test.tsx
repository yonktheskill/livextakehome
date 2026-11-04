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
    vi.stubGlobal("fetch", (url: string, init: RequestInit) =>
      url === "/api/auth/config" ? Promise.resolve(json({ registration_enabled: true })) : fetcher(url, init),
    );
    render(
      <AuthProvider>
        <AuthForm mode="register" />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Create account" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "new@example.test" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "example-passphrase" } });
    expect(screen.getByLabelText("Password")).toHaveAttribute("minlength", "10");
    expect(screen.getByLabelText("Password")).toHaveAttribute("maxlength", "256");
    const form = screen.getByRole("button", { name: "Create account" }).closest("form")!;
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "different-passphrase" },
    });
    fireEvent.submit(form);
    expect(screen.getByRole("alert")).toHaveTextContent("Passwords do not match.");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(replace).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "example-passphrase" },
    });
    fireEvent.submit(form);
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    expect(fetcher.mock.calls[1][0]).toBe("/api/auth/register");
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({
      email: "new@example.test",
      password: "example-passphrase",
    });
    expect(storage).not.toHaveBeenCalled();
  });
  it.each(["login", "register"] as const)("closes sign-up in %s mode", async (mode) => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(
          url === "/api/auth/config"
            ? json({ registration_enabled: false })
            : json({ detail: "Not signed in" }, 401),
        ),
      ),
    );
    render(
      <AuthProvider>
        <AuthForm mode={mode} />
      </AuthProvider>,
    );
    if (mode === "register") {
      expect(await screen.findByText("Registrations are closed.")).toBeInTheDocument();
      expect(screen.queryByLabelText("Email address")).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    } else {
      expect(await screen.findByRole("button", { name: "Sign in" })).toBeEnabled();
    }
    expect(screen.queryByRole("link", { name: "Create an account" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
  });
  it("keeps sign-up closed if the configuration request fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        Promise.resolve(json({ detail: "Unavailable" }, url === "/api/auth/config" ? 503 : 401)),
      ),
    );
    render(
      <AuthProvider>
        <AuthForm mode="register" />
      </AuthProvider>,
    );
    expect(await screen.findByText(/Unable to check registration availability/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
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

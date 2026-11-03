import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { Field, Label } from "@headlessui/react";
import { describe, expect, it, vi } from "vitest";
import { SecretInput } from "../components/secret-input";

describe("secret visibility", () => {
  it("toggles each field independently without changing values or submitting", () => {
    const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
    render(
      <form onSubmit={submit}>
        <Field>
          <Label>Password</Label>
          <SecretInput name="password" visibilityLabel="password" defaultValue="my-passphrase" />
        </Field>
        <Field>
          <Label>Confirm password</Label>
          <SecretInput name="confirmPassword" visibilityLabel="confirm password" />
        </Field>
      </form>,
    );
    const password = screen.getByLabelText("Password");
    const confirmation = screen.getByLabelText("Confirm password");
    expect(password).toHaveAttribute("type", "password");
    expect(confirmation).toHaveAttribute("type", "password");
    const toggle = screen.getByRole("button", { name: "Show password" });
    expect(toggle).toHaveAttribute("aria-controls", password.id);
    fireEvent.click(toggle);
    expect(password).toHaveAttribute("type", "text");
    expect(password).toHaveValue("my-passphrase");
    expect(confirmation).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Hide password" }));
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveValue("my-passphrase");
    expect(submit).not.toHaveBeenCalled();
  });
});

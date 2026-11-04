"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Button, Field, Input, Label } from "@headlessui/react";
import { ArrowRight } from "lucide-react";
import { useAuth } from "./auth";
import { SecretInput } from "./secret-input";
import { Brand } from "./brand";
import { api } from "../lib/api";
export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const { user, loading, authenticate } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [registrationEnabled, setRegistrationEnabled] = useState<boolean | null>(null);
  const [configError, setConfigError] = useState(false);
  useEffect(() => {
    let active = true;
    api<{ registration_enabled: boolean }>("/auth/config")
      .then((config) => {
        if (active) setRegistrationEnabled(config.registration_enabled === true);
      })
      .catch(() => {
        if (active) setConfigError(true);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (user) router.replace("/");
  }, [user, router]);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode === "register" && registrationEnabled !== true) return;
    setError("");
    const form = new FormData(event.currentTarget);
    if (mode === "register" && form.get("password") !== form.get("confirmPassword")) {
      setError("Passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await authenticate(mode, String(form.get("email")).trim(), String(form.get("password")));
      router.replace("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to sign in. Please try again.");
    } finally {
      setBusy(false);
    }
  }
  const register = mode === "register";
  return (
    <main className="auth-shell">
      <section className="auth-story">
        <Brand />
        <div>
          <span className="eyebrow">A LITTLE SPACE IN YOUR DAY</span>
          <h1>
            Your schedule,
            <br />
            in a conversation.
          </h1>
          <p>
            Find a time. Make a plan. Keep things moving.
            <br />A thoughtful assistant for your Cal.com bookings.
          </p>
        </div>
        <p className="story-foot">Less back and forth. More room for what matters.</p>
      </section>
      <section className="auth-panel">
        <div className="auth-card">
          <span className="eyebrow">YOUR TIME, WELL SPENT</span>
          <h2>
            {register && registrationEnabled === false
              ? "Registrations are closed."
              : register
                ? "Make room for a better day."
                : "Welcome back."}
          </h2>
          <p className="muted">
            {register
              ? configError
                ? "Unable to check registration availability. Please reload to try again."
                : registrationEnabled === null
                  ? "Checking registration availability…"
                  : registrationEnabled
                    ? "Create an account, then connect your Cal.com account."
                    : "New registrations are currently disabled. You can still sign in to an existing account."
              : "Sign in to pick up your conversation."}
          </p>
          {(!register || registrationEnabled === true) && (
            <form onSubmit={submit} className="form-stack">
              <Field>
                <Label>Email address</Label>
                <Input
                  className="input"
                  name="email"
                  type="email"
                  autoComplete="email"
                  required
                  maxLength={254}
                  placeholder="you@company.com"
                />
              </Field>
              <Field>
                <Label>Password</Label>
                <SecretInput
                  visibilityLabel="password"
                  name="password"
                  autoComplete={register ? "new-password" : "current-password"}
                  required
                  minLength={10}
                  maxLength={256}
                />
                {register && <p className="field-hint">Use at least 10 characters.</p>}
              </Field>
              {register && (
                <Field>
                  <Label>Confirm password</Label>
                  <SecretInput
                    visibilityLabel="confirm password"
                    name="confirmPassword"
                    autoComplete="new-password"
                    required
                    minLength={10}
                    maxLength={256}
                  />
                </Field>
              )}
              {error && (
                <p role="alert" className="error-box">
                  {error}
                </p>
              )}
              <Button className="primary-button" type="submit" disabled={busy || loading || !!user}>
                {busy ? "One moment…" : register ? "Create account" : "Sign in"}
                <ArrowRight size={17} />
              </Button>
            </form>
          )}
          {(register || registrationEnabled === true) && (
            <p className="auth-switch">
              {register ? "Already have an account?" : "New to HelpBook?"}{" "}
              <Link href={register ? "/login" : "/register"}>
                {register ? "Sign in" : "Create an account"}
              </Link>
            </p>
          )}
          <p className="fine-print">
            Your Cal.com API key stays on the server. You can disconnect it at any time.
          </p>
        </div>
      </section>
    </main>
  );
}

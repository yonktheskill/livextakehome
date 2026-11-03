"use client";
import { Button } from "@headlessui/react";
import { Check } from "lucide-react";
import type { Receipt } from "../lib/use-conversation";
export function ActionReceipt({ receipt }: { receipt: Receipt }) {
  // Only explicit, user-facing fields are rendered. Never stringify arbitrary tool payloads.
  const details =
    receipt.details && typeof receipt.details === "object"
      ? (receipt.details as Record<string, unknown>)
      : {};
  const allowed = ["title", "status", "start", "end", "time_zone", "duration_minutes", "booking_uid"];
  return (
    <article className="receipt">
      <div className="receipt-heading">
        <Check size={16} />
        <span>{receipt.kind.replaceAll("_", " ")}</span>
      </div>
      <p>{receipt.summary}</p>
      <dl>
        {allowed.map((key) =>
          typeof details[key] === "string" || typeof details[key] === "number" ? (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd>{String(details[key])}</dd>
            </div>
          ) : null,
        )}
      </dl>
    </article>
  );
}
const prompts = [
  { label: "View bookings", text: "What bookings do I have tomorrow?" },
  { label: "Book a meeting", text: "Help me book a meeting." },
  { label: "Reschedule", text: "Help me reschedule a booking." },
];

export function Welcome({ onPrompt }: { onPrompt: (text: string) => void }) {
  return (
    <section className="welcome">
      <h1>What’s on your calendar?</h1>
      <div className="suggestions">
        {prompts.map(({ label, text }) => (
          <Button
            key={label}
            onClick={() => {
              onPrompt(text);
            }}
            className="suggestion"
          >
            {label}
          </Button>
        ))}
      </div>
    </section>
  );
}

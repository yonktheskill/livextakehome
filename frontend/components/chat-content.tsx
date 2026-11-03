"use client";
import { Button } from "@headlessui/react";
import { ArrowUp, CalendarDays, Check, Clock3, Plus, Sun } from "lucide-react";
import type { Receipt } from "../lib/use-conversation";
export function ActionReceipt({ receipt }: { receipt: Receipt }) {
  // Only explicit, user-facing fields are rendered. Never stringify arbitrary tool payloads.
  const details =
    receipt.details && typeof receipt.details === "object" ? (receipt.details as Record<string, unknown>) : {};
  const allowed = ["title", "status", "start", "end", "time_zone", "duration_minutes", "booking_uid"];
  return (
    <article className="receipt">
      <div className="receipt-heading">
        <Check size={16} />
        <span>Action receipt · {receipt.kind.replaceAll("_", " ")}</span>
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
  { icon: CalendarDays, label: "See what's coming up", text: "What bookings do I have tomorrow?" },
  { icon: Plus, label: "Find a time to meet", text: "Help me book a meeting." },
  { icon: Clock3, label: "Make a little room", text: "Help me reschedule a booking." },
];

export function Welcome({ onPrompt }: { onPrompt: (text: string) => void }) {
  return (
    <section className="welcome">
      <div className="welcome-sun">
        <Sun size={34} strokeWidth={1.4} />
      </div>
      <span className="eyebrow">A LITTLE HELP WITH YOUR DAY</span>
      <h2>
        What can I take
        <br />
        off your calendar mind?
      </h2>
      <p>
        Book a meeting, see what’s ahead, or move things around.
        <br className="desktop-break" /> Just tell me what you have in mind.
      </p>
      <div className="suggestions">
        {prompts.map(({ icon: Icon, label, text }) => (
          <Button
            key={label}
            onClick={() => {
              onPrompt(text);
            }}
            className="suggestion"
          >
            <Icon size={19} />
            <span>{label}</span>
            <ArrowUp size={15} />
          </Button>
        ))}
      </div>
      <p className="welcome-detail">Real availability. Clear details. Cancellations confirmed with you.</p>
    </section>
  );
}

import type { Message, Receipt } from "./api";

type TimelineItem = { type: "message"; message: Message } | { type: "receipt"; receipt: Receipt };

function timestamp(value?: string) {
  if (!value) return Number.POSITIVE_INFINITY;
  // Persisted timestamps include microseconds. Date.parse alone drops that
  // precision and can swap a receipt and reply created in the same millisecond.
  const milliseconds = Date.parse(value);
  const fraction = value.match(/\.(\d+)/)?.[1] ?? "0";
  return Number.isNaN(milliseconds)
    ? Number.POSITIVE_INFINITY
    : Math.floor(milliseconds / 1000) + Number(`0.${fraction}`);
}

export function conversationTimeline(messages: Message[], receipts: Receipt[]): TimelineItem[] {
  const cards = receipts
    .filter((receipt) => receipt.kind === "create_booking")
    .sort((a, b) => {
      const time = timestamp(a.created_at) - timestamp(b.created_at);
      const aId = a.message_id ?? a.action_id;
      const bId = b.message_id ?? b.action_id;
      return time || (aId < bId ? -1 : aId > bId ? 1 : 0);
    });
  const items: TimelineItem[] = [];
  let next = 0;
  // Preserve the server's message order, including optimistic user messages.
  for (const message of messages) {
    if (message.role !== "user" && message.role !== "assistant") continue;
    while (next < cards.length) {
      const card = cards[next];
      const cardTime = timestamp(card.created_at);
      const messageTime = timestamp(message.created_at);
      if (
        cardTime < messageTime ||
        (cardTime === messageTime && card.message_id && card.message_id < message.id)
      ) {
        items.push({ type: "receipt", receipt: card });
        next++;
      } else break;
    }
    items.push({ type: "message", message });
  }
  return [...items, ...cards.slice(next).map((receipt) => ({ type: "receipt" as const, receipt }))];
}

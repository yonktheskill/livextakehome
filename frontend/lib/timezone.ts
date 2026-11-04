export function messageTime(timestamp: string, timeZone: string) {
  const date = new Date(timestamp);
  const options = { hour: "numeric", minute: "2-digit" } as const;
  try {
    return date.toLocaleTimeString([], { ...options, timeZone });
  } catch {
    // A bad imported timezone must not prevent the user opening the picker to correct it.
    return date.toLocaleTimeString([], { ...options, timeZone: "UTC" });
  }
}

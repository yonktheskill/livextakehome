export type StreamEvent = { id: number; type: string; data: Record<string, unknown> };
/** Incremental SSE framing: UTF-8 decoding belongs to the caller's TextDecoder. */
export class SSEParser {
  private buffer = "";
  private event = "message";
  private id = "";
  private data: string[] = [];
  constructor(private emit: (event: StreamEvent) => void) {}
  feed(chunk: string) {
    this.buffer += chunk;
    let index: number;
    while ((index = this.buffer.search(/[\r\n]/)) >= 0) {
      // A trailing CR might be the first half of a CRLF split across chunks.
      if (this.buffer[index] === "\r" && index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, index);
      const length = this.buffer[index] === "\r" && this.buffer[index + 1] === "\n" ? 2 : 1;
      this.buffer = this.buffer.slice(index + length);
      this.line(line);
    }
  }
  private line(line: string) {
    if (!line) {
      if (this.data.length && /^\d+$/.test(this.id)) {
        // Fail closed: reconnect from the last delivered ID rather than skip a corrupt event.
        const data: unknown = JSON.parse(this.data.join("\n"));
        const id = Number(this.id);
        if (!data || typeof data !== "object" || Array.isArray(data) || !Number.isSafeInteger(id)) {
          throw new Error("Invalid stream event.");
        }
        this.emit({ id, type: this.event, data: data as Record<string, unknown> });
      }
      this.data = [];
      this.event = "message";
      this.id = "";
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    if (field === "event") this.event = value;
    if (field === "id" && !value.includes("\0")) this.id = value;
  }
}
export const terminalEvents = new Set(["run.waiting", "run.completed", "run.failed", "run.interrupted"]);

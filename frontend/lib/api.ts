export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
    headers: { ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : `Request failed (${response.status}). Please try again.`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export type User = { id: string | number; email: string; time_zone: string };
export type Connection = { connected: boolean; label: string | null };
export type Attachment = {
  id: string;
  mime_type?: string;
  width?: number;
  height?: number;
  size_bytes?: number;
};
export type Message = {
  confirmation?: { message_id: string; content: string };
  source?: "voice";
  version?: number;
  attachments?: Attachment[];
  id: string;
  role: string;
  content: string;
  created_at: string;
  status: string;
};
export type Receipt = {
  action_id: string;
  message_id?: string | null;
  created_at?: string;
  kind: string;
  summary: string;
  details: unknown;
};
export type History = {
  messages: Message[];
  next_cursor: string | null;
  receipts?: Receipt[];
};

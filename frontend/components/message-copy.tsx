"use client";

import { useEffect, useState } from "react";
import { Button } from "@headlessui/react";
import { Check, Copy } from "lucide-react";

export function MessageCopy({ content }: { content: string }) {
  const [status, setStatus] = useState<"idle" | "copying" | "copied" | "error">("idle");

  useEffect(() => {
    if (status !== "copied" && status !== "error") return;
    const timeout = setTimeout(() => setStatus("idle"), 2500);
    return () => clearTimeout(timeout);
  }, [status]);

  async function copy() {
    setStatus("copying");
    try {
      await navigator.clipboard.writeText(content);
      setStatus("copied");
    } catch {
      setStatus("error");
    }
  }

  return (
    <div className="message-actions">
      <Button
        type="button"
        className="message-copy"
        aria-label="Copy message"
        disabled={status === "copying"}
        onClick={() => void copy()}
      >
        {status === "copied" ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
        {status === "copied" ? "Copied" : "Copy"}
      </Button>
      <span className={status === "error" ? "message-copy-error" : "sr-only"} role="status">
        {status === "copied"
          ? "Message copied to clipboard."
          : status === "error"
            ? "Unable to copy. Please try again."
            : ""}
      </span>
    </div>
  );
}

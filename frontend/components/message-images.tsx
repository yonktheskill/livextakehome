"use client";
/* eslint-disable @next/next/no-img-element */
import { useState } from "react";
import { Button, Dialog, DialogPanel, DialogTitle } from "@headlessui/react";
import type { Attachment } from "../lib/api";

export function MessageImages({
  attachments = [],
  onLoad,
}: {
  attachments?: Attachment[];
  onLoad?: () => void;
}) {
  const [selected, setSelected] = useState<Attachment | null>(null);
  const url = (id: string) => `/api/chat/attachments/${encodeURIComponent(id)}`;
  return (
    <>
      <div className="message-images">
        {attachments.map((attachment, i) => (
          <Button
            key={attachment.id}
            className="message-image-button"
            onClick={() => setSelected(attachment)}
            aria-label={`View attached image ${i + 1}`}
          >
            <img
              src={url(attachment.id)}
              alt={`Attached image ${i + 1}`}
              width={attachment.width ?? 240}
              height={attachment.height ?? 180}
              onLoad={onLoad}
              loading="lazy"
              referrerPolicy="no-referrer"
            />
          </Button>
        ))}
      </div>
      <Dialog open={!!selected} onClose={() => setSelected(null)} className="image-dialog">
        <div className="image-dialog-backdrop" />
        <div className="image-dialog-wrap">
          <DialogPanel className="image-dialog-panel">
            <DialogTitle className="sr-only">Attached image</DialogTitle>
            <Button className="image-close" onClick={() => setSelected(null)}>
              Close image
            </Button>
            {selected && (
              <img src={url(selected.id)} alt="Attached image, full view" referrerPolicy="no-referrer" />
            )}
          </DialogPanel>
        </div>
      </Dialog>
    </>
  );
}

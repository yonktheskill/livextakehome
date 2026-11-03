"use client";
import { useState } from "react";
import { Button, Dialog, DialogPanel, DialogTitle, Description, Field, Input, Label } from "@headlessui/react";
import { KeyRound, X } from "lucide-react";
import { api, ApiError, type Connection } from "../lib/api";
export function ConnectionDialog({
  open,
  onClose,
  connection,
  onChange,
  onUnauthorized,
}: {
  open: boolean;
  onClose: () => void;
  connection: Connection | null;
  onChange: (value: Connection) => void;
  onUnauthorized: () => void;
}) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirm, setConfirm] = useState(false);
  async function change(disconnect: boolean) {
    setBusy(true);
    setError("");
    try {
      if (disconnect) {
        await api("/connection", { method: "DELETE" });
        onChange({ connected: false, label: null });
      } else
        onChange(
          await api<Connection>("/connection", { method: "PUT", body: JSON.stringify({ api_key: key.trim() }) }),
        );
      setKey("");
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onUnauthorized();
      else setError(e instanceof Error ? e.message : "Unable to update connection.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      className="dialog-root"
    >
      <div className="dialog-backdrop" aria-hidden="true" />
      <div className="dialog-position">
        <DialogPanel className="dialog-panel">
          <div className="dialog-top">
            <span className="dialog-icon">
              <KeyRound size={22} />
            </span>
            <Button onClick={onClose} disabled={busy} className="icon-button" aria-label="Close connection settings">
              <X size={20} />
            </Button>
          </div>
          <DialogTitle className="dialog-title">Your Cal.com connection</DialogTitle>
          <Description className="muted">
            Connect your account so Daylight can check availability and manage your bookings.
          </Description>
          {connection?.connected && (
            <div className="connection-current">
              <span className="status-dot" />
              Connected {connection.label && <code>{connection.label}</code>}
            </div>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void change(false);
            }}
            className="form-stack"
          >
            <Field>
              <Label>{connection?.connected ? "Replace API key" : "Cal.com API key"}</Label>
              <Input
                className="input"
                type="password"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                required
                maxLength={4096}
                placeholder="Paste your API key"
              />
              <p className="field-hint">
                Find your key in Cal.com → Settings → Developer → API keys. Your key is sent securely to the backend and
                never saved in browser storage.
              </p>
            </Field>
            {error && (
              <p className="error-box" role="alert">
                {error}
              </p>
            )}
            <Button className="primary-button" disabled={busy || !key.trim()} type="submit">
              {busy ? "Updating…" : connection?.connected ? "Update connection" : "Connect Cal.com"}
            </Button>
          </form>
          {connection?.connected && (
            <div className="disconnect-area">
              {confirm ? (
                <>
                  <p>
                    Disconnect Cal.com? Your chat history stays available. Pending work will be interrupted; dispatched
                    actions may still finish.
                  </p>
                  <div className="button-row">
                    <Button className="danger-button" disabled={busy} onClick={() => void change(true)}>
                      Disconnect
                    </Button>
                    <Button className="text-button" disabled={busy} onClick={() => setConfirm(false)}>
                      Keep connection
                    </Button>
                  </div>
                </>
              ) : (
                <Button className="text-button danger" onClick={() => setConfirm(true)}>
                  Disconnect account
                </Button>
              )}
            </div>
          )}
        </DialogPanel>
      </div>
    </Dialog>
  );
}

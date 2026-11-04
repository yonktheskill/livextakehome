"use client";
import { Button, Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import { ChevronDown, KeyRound, LogOut } from "lucide-react";
import type { Connection, User } from "../lib/api";
import { Brand } from "./brand";
import { TimezonePicker } from "./timezone-picker";

export function ChatHeader({
  user,
  connection,
  loggingOut,
  onConnect,
  onSignOut,
  onTimeZoneChange,
  onUnauthorized,
}: {
  user: User;
  connection: Connection | null;
  loggingOut: boolean;
  onConnect: () => void;
  onSignOut: () => Promise<void>;
  onTimeZoneChange: (timeZone: string) => Promise<void>;
  onUnauthorized: () => void;
}) {
  return (
    <header className="chat-header">
      <Brand />
      <div className="header-actions">
        <TimezonePicker value={user.time_zone} onChange={onTimeZoneChange} onUnauthorized={onUnauthorized} />
        <Menu>
          <MenuButton className="account-button" aria-label={`User menu for ${user.email}`}>
            <span className="avatar" aria-hidden="true">
              {user.email[0].toUpperCase()}
            </span>
            <span className="account-email">{user.email}</span>
            <ChevronDown size={15} className="account-chevron" aria-hidden="true" />
          </MenuButton>
          <MenuItems anchor="bottom end" className="account-menu">
            <MenuItem>
              <Button className="menu-item" onClick={onConnect}>
                <KeyRound size={16} aria-hidden="true" />
                <span className="connection-menu-label">
                  <span>Cal.com connection</span>
                  <small>
                    <span
                      className={`status-dot ${connection?.connected ? "" : "offline"}`}
                      aria-hidden="true"
                    />
                    {connection?.connected
                      ? "Connected"
                      : connection === null
                        ? "Checking…"
                        : "Not connected"}
                  </small>
                </span>
              </Button>
            </MenuItem>
            <MenuItem>
              <Button disabled={loggingOut} className="menu-item" onClick={() => void onSignOut()}>
                <LogOut size={16} aria-hidden="true" />
                {loggingOut ? "Signing out…" : "Sign out"}
              </Button>
            </MenuItem>
          </MenuItems>
        </Menu>
      </div>
    </header>
  );
}

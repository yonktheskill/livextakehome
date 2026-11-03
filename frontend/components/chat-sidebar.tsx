"use client";
import { Button, Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import { ChevronDown, LogOut } from "lucide-react";
import type { Connection, User } from "../lib/api";
import { Brand } from "./brand";
export function ChatSidebar({
  user,
  connection,
  loggingOut,
  onConnect,
  onSignOut,
}: {
  user: User;
  connection: Connection | null;
  loggingOut: boolean;
  onConnect: () => void;
  onSignOut: () => Promise<void>;
}) {
  return (
    <aside className="sidebar">
      <Brand />
      <div className="active-nav">Chat</div>
      <div className="sidebar-bottom">
        <Button className="connection-card" onClick={() => onConnect()}>
          <span>
            <strong>Cal.com</strong>
            <small>
              {connection?.connected
                ? connection.label || "Connected"
                : connection === null
                  ? "Checking…"
                  : "Connect"}
            </small>
          </span>
          <span className={`status-dot ${connection?.connected ? "" : "offline"}`} />
        </Button>
        <Menu>
          <MenuButton className="account-button">
            <span className="avatar">{user.email[0].toUpperCase()}</span>
            <span className="account-email">{user.email}</span>
            <ChevronDown size={15} />
          </MenuButton>
          <MenuItems anchor="top start" className="account-menu">
            <MenuItem>
              <Button disabled={loggingOut} className="menu-item" onClick={() => void onSignOut()}>
                <LogOut size={16} />
                {loggingOut ? "Signing out…" : "Sign out"}
              </Button>
            </MenuItem>
          </MenuItems>
        </Menu>
      </div>
    </aside>
  );
}

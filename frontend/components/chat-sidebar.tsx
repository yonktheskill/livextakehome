"use client";
import Link from "next/link";
import { Button, Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import { CalendarDays, ChevronDown, KeyRound, LogOut, MessageSquare, Sun } from "lucide-react";
import type { Connection, User } from "../lib/api";
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
      <Link href="/" className="brand">
        <span className="brand-icon">
          <CalendarDays size={23} />
        </span>
        daylight<span className="brand-dot">.</span>
      </Link>
      <div className="workspace-label">YOUR WORKSPACE</div>
      <div className="active-nav">
        <MessageSquare size={18} />
        <span>Your conversation</span>
        <span className="nav-dot" />
      </div>
      <div className="sidebar-note">
        <Sun size={23} />
        <h2>A calmer kind of calendar.</h2>
        <p>Ask naturally. We’ll work through the details together.</p>
      </div>
      <div className="sidebar-bottom">
        <Button className="connection-card" onClick={() => onConnect()}>
          <span className="connection-card-icon">
            <KeyRound size={18} />
          </span>
          <span>
            <strong>Cal.com</strong>
            <small>
              {connection?.connected
                ? connection.label || "Connected"
                : connection === null
                  ? "Check connection"
                  : "Connect your account"}
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

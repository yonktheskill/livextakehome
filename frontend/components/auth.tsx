"use client";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, ApiError, type User } from "../lib/api";
const AuthContext = createContext<{
  user: User | null;
  loading: boolean;
  error: string;
  refresh: () => Promise<void>;
  refreshProfile: () => Promise<void>;
  authenticate: (mode: "login" | "register", email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  updateTimeZone: (timeZone: string) => Promise<void>;
} | null>(null);
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setUser(await api<User>("/auth/me"));
    } catch (e) {
      setUser(null);
      if (!(e instanceof ApiError && e.status === 401))
        setError(e instanceof Error ? e.message : "Unable to check your session.");
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    let active = true;
    api<User>("/auth/me")
      .then((value) => {
        if (active) setUser(value);
      })
      .catch((e) => {
        if (active && !(e instanceof ApiError && e.status === 401))
          setError(e instanceof Error ? e.message : "Unable to check your session.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  async function refreshProfile() {
    try {
      const updated = await api<User>("/auth/me");
      setUser((current) => (current?.id === updated.id ? updated : current));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setUser(null);
      else throw e;
    }
  }
  async function authenticate(mode: "login" | "register", email: string, password: string) {
    setUser(await api<User>(`/auth/${mode}`, { method: "POST", body: JSON.stringify({ email, password }) }));
  }
  async function logout() {
    await api("/auth/logout", { method: "POST" });
    if (user) {
      try {
        sessionStorage.removeItem(`daylight-pending:${user.id}`);
        sessionStorage.removeItem(`daylight-run:${user.id}`);
      } catch {}
    }
    setUser(null);
  }
  async function updateTimeZone(timeZone: string) {
    const updated = await api<User>("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ time_zone: timeZone }),
    });
    setUser((current) => (current?.id === updated.id ? updated : current));
  }
  return (
    <AuthContext.Provider
      value={{ user, loading, error, refresh, refreshProfile, authenticate, logout, updateTimeZone }}
    >
      {children}
    </AuthContext.Provider>
  );
}
export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("AuthProvider is required");
  return value;
}

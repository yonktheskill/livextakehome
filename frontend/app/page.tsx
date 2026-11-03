"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@headlessui/react";
import { useAuth } from "../components/auth";
import { Chat } from "../components/chat";
export default function Page() {
  const { user, loading, error, refresh } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (!loading && !user && !error) router.replace("/login");
  }, [loading, user, error, router]);
  if (loading)
    return (
      <main className="page-loading" role="status">
        <span className="loading-orbit" />
        Opening Daylight…
      </main>
    );
  if (error)
    return (
      <main className="page-loading">
        <p role="alert">{error}</p>
        <Button className="primary-button" onClick={() => void refresh()}>
          Try again
        </Button>
      </main>
    );
  return user ? (
    <Chat key={user.id} user={user} />
  ) : (
    <main className="page-loading" role="status">
      Taking you to sign in…
    </main>
  );
}

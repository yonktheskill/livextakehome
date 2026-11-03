import type { Metadata } from "next";
import { AuthProvider } from "../components/auth";
import "./globals.css";
export const metadata: Metadata = {
  title: "HelpBook · Your scheduling assistant",
  description: "Make room for your day. Manage your Cal.com bookings in a conversation.",
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}

import Link from "next/link";
import { BookOpen } from "lucide-react";

export function Brand({ className = "" }: { className?: string }) {
  return (
    <Link href="/" className={`brand ${className}`.trim()}>
      <span className="brand-icon" aria-hidden="true">
        <BookOpen size={22} />
      </span>
      HelpBook
    </Link>
  );
}

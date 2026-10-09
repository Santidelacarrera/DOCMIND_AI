"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api } from "../lib/api";

export default function Nav({ current }: { current?: string }) {
  const router = useRouter();
  async function logout() {
    await api("/api/v1/auth/logout", { method: "POST" });
    router.replace("/login");
  }
  return (
    <nav aria-label="Primary">
      <Link href="/dashboard">DocMind AI</Link>
      <Link href="/documents" aria-current={current === "documents" ? "page" : undefined}>
        Documents
      </Link>
      <Link href="/schemas" aria-current={current === "schemas" ? "page" : undefined}>
        Schemas
      </Link>
      <Link href="/members" aria-current={current === "members" ? "page" : undefined}>
        Members
      </Link>
      <Link href="/webhooks" aria-current={current === "webhooks" ? "page" : undefined}>
        Webhooks
      </Link>
      <button type="button" className="linkbutton" onClick={logout}>
        Sign out
      </button>
    </nav>
  );
}

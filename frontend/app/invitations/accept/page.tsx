"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "../../../lib/api";

export default function AcceptInvitation() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const [status, setStatus] = useState<"checking" | "needs-auth" | "accepting" | "done" | "error">(
    token ? "checking" : "error",
  );
  const [message, setMessage] = useState(
    token ? "" : "This invitation link is missing its token.",
  );

  useEffect(() => {
    if (!token) return;
    void (async () => {
      const me = await api("/api/v1/auth/me");
      if (!me.ok) {
        setStatus("needs-auth");
        return;
      }
      setStatus("accepting");
      const res = await api("/api/v1/invitations/accept", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      if (res.ok) {
        setStatus("done");
        setTimeout(() => router.replace("/dashboard"), 1500);
      } else {
        const body = await res.json().catch(() => ({}));
        setStatus("error");
        setMessage(
          body.detail === "INVITATION_EMAIL_MISMATCH"
            ? "You're signed in with a different email address than this invitation was sent to."
            : body.detail === "INVITATION_EXPIRED"
              ? "This invitation has expired. Ask an admin to send a new one."
              : "This invitation link is no longer valid.",
        );
      }
    })();
  }, [token, router]);

  return (
    <main>
      <section className="panel">
        <h1>Join a DocMind AI workspace</h1>
        {status === "checking" && <p>Checking your invitation…</p>}
        {status === "accepting" && <p>Joining the workspace…</p>}
        {status === "done" && <p role="status">You&apos;re in! Redirecting to the dashboard…</p>}
        {status === "needs-auth" && (
          <p>
            Log in or create an account with the email address this invitation was sent to, then{" "}
            <Link href={`/login?next=/invitations/accept?token=${encodeURIComponent(token)}`}>
              come back and open this link again
            </Link>
            .
          </p>
        )}
        {status === "error" && <p role="alert">{message}</p>}
      </section>
    </main>
  );
}

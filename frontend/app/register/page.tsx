"use client";
import Link from "next/link";
import { FormEvent, useState } from "react";
import { api } from "../../lib/api";
import { useRouter } from "next/navigation";
export default function Register() {
  const [email, setEmail] = useState("");
  const [organization_name, setOrganization] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    setLoading(true);
    setError("");
    const res = await api("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, organization_name }),
    });
    setLoading(false);
    if (res.ok) router.replace("/dashboard");
    else
      setError(
        res.status === 409
          ? "This email is already registered."
          : "Use a valid email and a password of at least 12 characters.",
      );
  }
  return (
    <main className="auth">
      <section className="panel">
        <p className="eyebrow">GET STARTED</p>
        <h1>Create workspace</h1>
        <form onSubmit={submit}>
          <label>
            Work email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label>
            Workspace name
            <input
              value={organization_name}
              onChange={(e) => setOrganization(e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          <label>
            Confirm password
            <input
              type="password"
              minLength={12}
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button disabled={loading}>
            {loading ? "Creating…" : "Create account"}
          </button>
        </form>
        <p>
          Already registered? <Link href="/login">Sign in</Link>
        </p>
      </section>
    </main>
  );
}

"use client";
import Link from "next/link";
import { FormEvent, useState } from "react";
import { api } from "../../lib/api";
import { useRouter } from "next/navigation";
export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  async function submit(e: FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    const response = await api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    setLoading(false);
    if (response.ok) router.replace("/dashboard");
    else
      setError(
        response.status === 401
          ? "Email or password is incorrect."
          : "Unable to sign in.",
      );
  }
  return (
    <main className="auth">
      <section className="panel">
        <p className="eyebrow">WELCOME BACK</p>
        <h1>Sign in</h1>
        <form onSubmit={submit}>
          <label>
            Email
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
            />
          </label>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <button disabled={loading}>
            {loading ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p>
          New to DocMind? <Link href="/register">Create an account</Link>
        </p>
      </section>
    </main>
  );
}

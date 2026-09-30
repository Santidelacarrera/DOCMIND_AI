export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  return document.cookie.split("; ").find((item) => item.startsWith("csrf_token="))?.split("=")[1];
}

export function csrfHeaders(): HeadersInit {
  const csrf = csrfToken();
  return csrf ? { "X-CSRF-Token": csrf } : {};
}

export async function api(path: string, init: RequestInit = {}) {
  return fetch(`${API_URL}${path}`, {
    credentials: "include", ...init,
    headers: { "Content-Type": "application/json", ...csrfHeaders(), ...init.headers },
  });
}

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const CSRF_STORAGE_KEY = "docmind_csrf";

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const fromCookie = document.cookie
    .split("; ")
    .find((item) => item.startsWith("csrf_token="))
    ?.split("=")[1];
  if (fromCookie) return fromCookie;
  try {
    return window.sessionStorage.getItem(CSRF_STORAGE_KEY) ?? undefined;
  } catch {
    return undefined;
  }
}

function storeCsrf(token: unknown) {
  if (typeof window === "undefined" || typeof token !== "string" || !token) return;
  try {
    window.sessionStorage.setItem(CSRF_STORAGE_KEY, token);
  } catch {
    // sessionStorage can be disabled in strict privacy modes
  }
}

function clearCsrf() {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(CSRF_STORAGE_KEY);
  } catch {
    // ignore
  }
}

export function csrfHeaders(): HeadersInit {
  const csrf = csrfToken();
  return csrf ? { "X-CSRF-Token": csrf } : {};
}

async function ensureCsrfToken(path: string, method: string) {
  if (csrfToken()) return;
  const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method.toUpperCase());
  const isBootstrap = path.startsWith("/api/v1/organizations");
  if (!isMutation && !isBootstrap) return;
  if (path.startsWith("/api/v1/auth/login") || path.startsWith("/api/v1/auth/register")) return;
  try {
    const me = await fetch(`${API_URL}/api/v1/auth/me`, { credentials: "include" });
    if (me.ok) {
      const body = (await me.json()) as { csrf_token?: string };
      storeCsrf(body.csrf_token);
    }
  } catch {
    // ignore network failure; the main request will report it
  }
}

export async function api(path: string, init: RequestInit = {}) {
  const method = (init.method ?? "GET").toUpperCase();
  await ensureCsrfToken(path, method);
  const response = await fetch(`${API_URL}${path}`, {
    credentials: "include",
    ...init,
    headers: { "Content-Type": "application/json", ...csrfHeaders(), ...init.headers },
  });
  if (response.ok) {
    if (path.startsWith("/api/v1/auth/logout")) {
      clearCsrf();
    } else if (
      path.startsWith("/api/v1/auth/login") ||
      path.startsWith("/api/v1/auth/register") ||
      path.startsWith("/api/v1/auth/me")
    ) {
      response
        .clone()
        .json()
        .then((body: { csrf_token?: string }) => storeCsrf(body?.csrf_token))
        .catch(() => {});
    }
  }
  return response;
}

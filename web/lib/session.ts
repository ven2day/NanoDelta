export const SESSION_COOKIE = "nanodelta_session";
const SESSION_TTL_SECONDS = 60 * 60 * 8;

export type Session = {
  subject: string;
  role: "viewer" | "operator" | "admin";
};

export function sessionCookie(value: string) {
  return {
    name: SESSION_COOKIE,
    value,
    httpOnly: true,
    // NODE_ENV is always "production" in the built image regardless of whether this
    // deployment actually sits behind TLS -- using it here made every plain-HTTP
    // deployment silently unusable: the browser accepts a Secure cookie over HTTP,
    // discards it without error, login appears to succeed, and every subsequent
    // request looks unauthenticated with no indication why. Default stays secure;
    // an operator who genuinely has no TLS in front of this yet (not recommended
    // beyond a first local check) can opt out explicitly.
    secure: process.env.NANODELTA_WEB_COOKIE_SECURE !== "false",
    sameSite: "strict" as const,
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  };
}

async function authRequest(path: string, init: RequestInit): Promise<Response> {
  const base = process.env.NANODELTA_BACKEND_URL;
  if (!base) throw new Error("NANODELTA_BACKEND_URL is not configured");
  return fetch(new URL(path, base), { ...init, cache: "no-store" });
}

export async function login(username: string, password: string): Promise<Session & { token: string }> {
  const response = await authRequest("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error(response.status === 401 ? "Invalid username or password" : "Authentication service unavailable");
  return response.json() as Promise<Session & { token: string }>;
}

export async function validateSession(token?: string): Promise<Session | null> {
  if (!token) return null;
  const response = await authRequest("/api/auth/session", {
    method: "GET", headers: { Authorization: `Bearer ${token}` },
  });
  return response.ok ? response.json() as Promise<Session> : null;
}

export async function revokeSession(token?: string): Promise<void> {
  if (!token) return;
  await authRequest("/api/auth/logout", {
    method: "POST", headers: { Authorization: `Bearer ${token}` },
  });
}

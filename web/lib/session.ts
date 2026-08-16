import { createHmac, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

export const SESSION_COOKIE = "nanodelta_session";
const SESSION_TTL_SECONDS = 60 * 60 * 8;

export type Session = {
  subject: string;
  role: "read" | "operator" | "admin";
  expiresAt: number;
};

function secret(): string {
  const path = process.env.NANODELTA_WEB_SESSION_SECRET_FILE;
  const value = process.env.NANODELTA_WEB_SESSION_SECRET ?? (path ? readFileSync(path, "utf8").trim() : "");
  if (!value || value.length < 32) {
    throw new Error("NANODELTA_WEB_SESSION_SECRET must contain at least 32 characters");
  }
  return value;
}

function signature(payload: string): string {
  return createHmac("sha256", secret()).update(payload).digest("base64url");
}

export function createSession(subject: string, role: Session["role"]): string {
  const payload = Buffer.from(
    JSON.stringify({ subject, role, expiresAt: Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS }),
  ).toString("base64url");
  return `${payload}.${signature(payload)}`;
}

export function parseSession(value?: string): Session | null {
  if (!value) return null;
  const [payload, supplied, extra] = value.split(".");
  if (!payload || !supplied || extra) return null;
  const expected = signature(payload);
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  if (left.length !== right.length || !timingSafeEqual(left, right)) return null;
  try {
    const parsed = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as Session;
    if (!parsed.subject || !["read", "operator", "admin"].includes(parsed.role)) return null;
    if (!Number.isInteger(parsed.expiresAt) || parsed.expiresAt <= Math.floor(Date.now() / 1000)) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function sessionCookie(value: string) {
  return {
    name: SESSION_COOKIE,
    value,
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict" as const,
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  };
}

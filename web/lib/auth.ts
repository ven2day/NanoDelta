import { createHash, createHmac, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";

export type Role = "viewer" | "operator" | "admin";
export type Session = { username: string; role: Role; expiresAt: number };
type UserRecord = { username: string; role: Role; salt: string; password_hash: string };

const COOKIE_NAME = "nanodelta_session";
const SESSION_SECONDS = 8 * 60 * 60;

function requiredFile(variable: string): Buffer {
  const path = process.env[variable];
  if (!path) throw new Error(`${variable} is not configured`);
  return readFileSync(path);
}

function sessionKey(): Buffer {
  const key = requiredFile("NANODELTA_SESSION_KEY_PATH");
  if (key.length < 32) throw new Error("session signing key must contain at least 32 bytes");
  return key;
}

function users(): UserRecord[] {
  const parsed: unknown = JSON.parse(requiredFile("NANODELTA_UI_USERS_PATH").toString("utf8"));
  if (!Array.isArray(parsed)) throw new Error("UI users file must contain an array");
  return parsed as UserRecord[];
}

function equalText(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function verifyCredentials(username: string, password: string): Session | null {
  const user = users().find((candidate) => equalText(candidate.username, username));
  if (!user || !["viewer", "operator", "admin"].includes(user.role)) return null;
  const derived = scryptSync(password, Buffer.from(user.salt, "hex"), 64).toString("hex");
  if (!equalText(derived, user.password_hash)) return null;
  return { username: user.username, role: user.role, expiresAt: Math.floor(Date.now() / 1000) + SESSION_SECONDS };
}

export function encodeSession(session: Session): string {
  const payload = Buffer.from(JSON.stringify(session)).toString("base64url");
  const signature = createHmac("sha256", sessionKey()).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

export function decodeSession(value: string | undefined): Session | null {
  if (!value) return null;
  const [payload, signature, extra] = value.split(".");
  if (!payload || !signature || extra) return null;
  const expected = createHmac("sha256", sessionKey()).update(payload).digest("base64url");
  if (!equalText(signature, expected)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")) as Session;
    if (!session.username || !["viewer", "operator", "admin"].includes(session.role)) return null;
    if (session.expiresAt <= Math.floor(Date.now() / 1000)) return null;
    return session;
  } catch {
    return null;
  }
}

export function backendApiKey(role: Role): string {
  const values = JSON.parse(requiredFile("NANODELTA_BACKEND_KEYS_PATH").toString("utf8")) as Record<string, string>;
  const key = values[role];
  if (!key) throw new Error(`no backend API key configured for ${role}`);
  return key;
}

export function newSalt(): string { return randomBytes(16).toString("hex"); }
export function passwordHash(password: string, salt: string): string {
  return scryptSync(password, Buffer.from(salt, "hex"), 64).toString("hex");
}
export function credentialFingerprint(username: string): string {
  return createHash("sha256").update(username).digest("hex").slice(0, 12);
}
export { COOKIE_NAME, SESSION_SECONDS };

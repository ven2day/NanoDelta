import { timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import { NextResponse } from "next/server";
import { createSession, sessionCookie, type Session } from "@/lib/session";

function equal(left: string, right: string): boolean {
  const a = Buffer.from(left);
  const b = Buffer.from(right);
  return a.length === b.length && timingSafeEqual(a, b);
}

export async function POST(request: Request) {
  const configured = async (name: string) => {
    const direct = process.env[name];
    if (direct) return direct;
    const path = process.env[`${name}_FILE`];
    return path ? (await readFile(path, "utf8")).trim() : "";
  };
  const expectedUser = await configured("NANODELTA_WEB_USERNAME");
  const expectedPassword = await configured("NANODELTA_WEB_PASSWORD");
  const configuredRole = process.env.NANODELTA_WEB_ROLE ?? "read";
  if (!expectedUser || !expectedPassword || !["read", "operator", "admin"].includes(configuredRole)) {
    return NextResponse.json({ error: "Web authentication is not configured" }, { status: 503 });
  }
  const body = (await request.json().catch(() => null)) as { username?: string; password?: string } | null;
  if (!body?.username || !body.password || !equal(body.username, expectedUser) || !equal(body.password, expectedPassword)) {
    return NextResponse.json({ error: "Invalid username or password" }, { status: 401 });
  }
  const response = NextResponse.json({ subject: expectedUser, role: configuredRole });
  response.cookies.set(sessionCookie(createSession(expectedUser, configuredRole as Session["role"])));
  return response;
}

import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { parseSession, SESSION_COOKIE } from "@/lib/session";

export async function GET() {
  const session = parseSession((await cookies()).get(SESSION_COOKIE)?.value);
  return session
    ? NextResponse.json({ subject: session.subject, role: session.role, expiresAt: session.expiresAt })
    : NextResponse.json({ error: "Authentication required" }, { status: 401 });
}

import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { validateSession, SESSION_COOKIE } from "@/lib/session";

export async function GET() {
  const session = await validateSession((await cookies()).get(SESSION_COOKIE)?.value);
  return session
    ? NextResponse.json({ subject: session.subject, username: session.username, role: session.role })
    : NextResponse.json({ error: "Authentication required" }, { status: 401 });
}

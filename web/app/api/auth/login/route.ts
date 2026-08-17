import { NextResponse } from "next/server";
import { login, sessionCookie } from "@/lib/session";

export async function POST(request: Request) {
  const body = (await request.json().catch(() => null)) as { username?: string; password?: string } | null;
  if (!body?.username || !body.password) {
    return NextResponse.json({ error: "Username and password are required" }, { status: 400 });
  }
  try {
    const session = await login(body.username, body.password);
    const response = NextResponse.json({ subject: session.subject, username: session.username, role: session.role });
    response.cookies.set(sessionCookie(session.token));
    return response;
  } catch (error) {
    return NextResponse.json({ error: error instanceof Error ? error.message : "Authentication failed" }, { status: 401 });
  }
}

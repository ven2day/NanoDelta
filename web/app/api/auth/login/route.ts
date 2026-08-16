import { NextResponse } from "next/server";
import { COOKIE_NAME, SESSION_SECONDS, encodeSession, verifyCredentials } from "../../../../lib/auth";

export async function POST(request: Request) {
  let body: { username?: string; password?: string };
  try { body = await request.json(); } catch { return NextResponse.json({ error: "invalid request" }, { status: 400 }); }
  const session = verifyCredentials(body.username?.trim() ?? "", body.password ?? "");
  if (!session) return NextResponse.json({ error: "invalid username or password" }, { status: 401 });
  const response = NextResponse.json({ username: session.username, role: session.role });
  response.cookies.set(COOKIE_NAME, encodeSession(session), {
    httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "strict",
    path: "/", maxAge: SESSION_SECONDS,
  });
  return response;
}

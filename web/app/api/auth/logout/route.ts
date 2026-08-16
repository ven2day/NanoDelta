import { NextResponse } from "next/server";
import { cookies } from "next/headers";
import { revokeSession, SESSION_COOKIE, sessionCookie } from "@/lib/session";

export async function POST() {
  try {
    await revokeSession((await cookies()).get(SESSION_COOKIE)?.value);
  } catch {
    // Clear the browser credential even when the backend is temporarily unavailable.
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set({ ...sessionCookie(""), name: SESSION_COOKIE, maxAge: 0 });
  return response;
}

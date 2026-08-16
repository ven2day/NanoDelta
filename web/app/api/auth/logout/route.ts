import { NextResponse } from "next/server";
import { SESSION_COOKIE, sessionCookie } from "@/lib/session";

export async function POST() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set({ ...sessionCookie(""), name: SESSION_COOKIE, maxAge: 0 });
  return response;
}

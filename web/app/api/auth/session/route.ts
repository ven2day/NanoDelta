import { NextResponse } from "next/server";
import { currentSession } from "../../../../lib/server-session";

export async function GET() {
  const session = await currentSession();
  if (!session) return NextResponse.json({ error: "not authenticated" }, { status: 401 });
  return NextResponse.json({ username: session.username, role: session.role, expiresAt: session.expiresAt });
}

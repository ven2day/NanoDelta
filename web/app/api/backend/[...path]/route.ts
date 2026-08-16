import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { allowlistedBackendPath, allowlistedBackendQuery, backendGet } from "@/lib/backend";
import { validateSession, SESSION_COOKIE } from "@/lib/session";

export async function GET(request: Request, context: { params: Promise<{ path: string[] }> }) {
  const session = await validateSession((await cookies()).get(SESSION_COOKIE)?.value);
  if (!session) {
    return NextResponse.json({ error: "Authentication required" }, { status: 401 });
  }
  const path = allowlistedBackendPath((await context.params).path);
  if (!path) return NextResponse.json({ error: "Backend route is not allowed" }, { status: 404 });
  try {
    const query = allowlistedBackendQuery(new URL(request.url).searchParams);
    const upstream = await backendGet(`${path}${query}`, session.role);
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") ?? "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Backend unavailable";
    return NextResponse.json({ error: message }, { status: 503 });
  }
}

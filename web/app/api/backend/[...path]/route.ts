import { NextRequest, NextResponse } from "next/server";
import { backendApiKey } from "../../../../lib/auth";
import { currentSession } from "../../../../lib/server-session";

const ALLOWED = [
  /^overview$/, /^finops(?:\/alerts)?$/, /^(nse|forex|crypto)\/health$/,
  /^(nse|forex|crypto)\/(features|strategies|agent-runs|decisions|paper\/positions|paper\/outcomes)$/,
  /^(nse|forex|crypto)\/history-status$/, /^decision-cycles\/[^/]+$/,
];

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const session = await currentSession();
  if (!session) return NextResponse.json({ error: "not authenticated" }, { status: 401 });
  const path = (await context.params).path.join("/");
  if (!ALLOWED.some((pattern) => pattern.test(path))) return NextResponse.json({ error: "endpoint not allowed" }, { status: 404 });
  const base = process.env.NANODELTA_API_URL;
  if (!base) return NextResponse.json({ error: "backend is not configured" }, { status: 503 });
  const target = new URL(`/api/${path}`, base);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));
  try {
    const upstream = await fetch(target, {
      headers: { "X-API-Key": backendApiKey(session.role), Accept: "application/json" },
      cache: "no-store", signal: AbortSignal.timeout(10_000),
    });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ error: "backend unavailable" }, { status: 503 });
  }
}

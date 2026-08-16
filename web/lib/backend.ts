import { readFile } from "node:fs/promises";

const SEGMENT = /^[a-z0-9-]+$/;
const MARKET_RESOURCES = new Set([
  "candles",
  "features",
  "universe",
  "session",
  "history-status",
  "orders",
  "trades",
  "positions",
  "decision-events",
  "signals",
  "risk/aggregate",
  "performance",
]);
const GLOBAL_RESOURCES = new Set(["overview", "finops", "finops/alerts", "alerts", "reports", "settings", "audit", "strategy-lab/strategies", "strategy-lab/validations"]);
const QUERY_PARAMETERS = new Set(["symbol", "timeframe", "stage", "status", "reason_code", "action", "state", "strategy_key", "cycle_id", "provider", "enabled", "market", "limit", "offset"]);
const MARKETS = new Set(["nse", "forex", "crypto"]);

export function allowlistedBackendPath(segments: string[]): string | null {
  if (!segments.length || segments.some((part) => !SEGMENT.test(part))) return null;
  const path = segments.join("/");
  if (GLOBAL_RESOURCES.has(path)) return `/api/${path}`;
  const [market, ...rest] = segments;
  const resource = rest.join("/");
  if (!MARKETS.has(market)) return null;
  if (resource === "health" || MARKET_RESOURCES.has(resource)) return `/api/${path}`;
  return null;
}

export function allowlistedBackendQuery(search: URLSearchParams): string {
  const filtered = new URLSearchParams();
  for (const [key, value] of search) if (QUERY_PARAMETERS.has(key)) filtered.append(key, value);
  const query = filtered.toString();
  return query ? `?${query}` : "";
}

async function apiKey(role: "viewer" | "operator" | "admin"): Promise<string> {
  const path = process.env.NANODELTA_BACKEND_KEYS_PATH;
  if (!path) throw new Error("NANODELTA_BACKEND_KEYS_PATH is not configured");
  const parsed = JSON.parse(await readFile(path, "utf8")) as Record<string, unknown>;
  const value = parsed[role];
  if (typeof value !== "string" || !value.trim()) throw new Error(`backend API key for ${role} is missing`);
  return value;
}

export async function backendGet(path: string, role: "viewer" | "operator" | "admin"): Promise<Response> {
  const base = process.env.NANODELTA_BACKEND_URL;
  if (!base) throw new Error("NANODELTA_BACKEND_URL is not configured");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8_000);
  try {
    return await fetch(new URL(path, base), {
      method: "GET",
      headers: { "X-API-Key": await apiKey(role), Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

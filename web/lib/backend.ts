import { readFile } from "node:fs/promises";

const SEGMENT = /^[a-z0-9-]+$/;
const COLLECTIONS = new Set([
  "features",
  "strategies",
  "agent-runs",
  "decisions",
  "paper/positions",
  "paper/outcomes",
]);
const MARKETS = new Set(["nse", "forex", "crypto"]);

export function allowlistedBackendPath(segments: string[]): string | null {
  if (!segments.length || segments.some((part) => !SEGMENT.test(part))) return null;
  const path = segments.join("/");
  if (path === "overview" || path === "finops" || path === "finops/alerts") return `/api/${path}`;
  const [market, ...rest] = segments;
  const resource = rest.join("/");
  if (!MARKETS.has(market)) return null;
  if (resource === "health" || COLLECTIONS.has(resource)) return `/api/${path}`;
  return null;
}

async function apiKey(role: "read" | "operator" | "admin"): Promise<string> {
  const prefix = `NANODELTA_BACKEND_${role.toUpperCase()}_API_KEY`;
  if (process.env[prefix]) return process.env[prefix];
  const path = process.env[`${prefix}_FILE`];
  if (!path) throw new Error(`${prefix}_FILE is not configured`);
  const value = (await readFile(path, "utf8")).trim();
  if (!value) throw new Error("backend API key file is empty");
  return value;
}

export async function backendGet(path: string, role: "read" | "operator" | "admin"): Promise<Response> {
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

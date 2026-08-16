import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
const backend = await readFile(new URL("../lib/backend.ts", import.meta.url), "utf8");
const proxy = await readFile(new URL("../app/api/backend/[...path]/route.ts", import.meta.url), "utf8");
const readme = await readFile(new URL("../README.md", import.meta.url), "utf8");

test("all NSE pages are composed from authoritative endpoints", () => {
  for (const name of ["Dashboard", "Universe", "Strategies", "Signals", "Decisions", "Positions", "Risk", "Backtests", "Reports", "Logs", "Settings"]) {
    assert.match(page, new RegExp(`\\b${name}\\b`));
  }
  for (const endpoint of ["nse/universe", "nse/strategy-validation/strategies", "nse/strategy-validation/backtests", "nse/signals", "nse/decision-events", "nse/positions", "nse/risk/aggregate", "nse/performance", "reports?market=nse", "audit?market=nse", "settings?market=nse"]) {
    assert.ok(page.includes(endpoint), `missing ${endpoint}`);
  }
});

test("the UI is fail-visible and contains no representative trading rows", () => {
  for (const value of ["SBIN", "RELIANCE", "₹20,18,420", "VWAP Pullback", "ALL SYSTEMS NORMAL"]) assert.ok(!page.includes(value));
  for (const state of ["loading", "error", "empty", "stale", "unavailable"]) assert.ok(page.includes(`kind=\"${state}\"`) || page.includes(`\"${state}\"`));
  assert.ok(page.includes("No genuine BUY / SELL candidates"));
  assert.ok(page.includes("no audited backend mutation contract exists"));
  assert.ok(page.includes("aria-disabled=\"true\""));
});

test("filters are useful and URL-persisted", () => {
  for (const filter of ["symbol", "timeframe", "strategy", "action", "decision", "status", "provider", "freshness", "date", "cycle"]) assert.ok(page.includes(`\"${filter}\"`));
  assert.ok(page.includes("window.history.replaceState"));
  assert.ok(page.includes("window.history.pushState"));
  assert.ok(page.includes("URLSearchParams"));
});

test("the browser proxy remains authenticated, GET-only and allowlisted", () => {
  assert.ok(proxy.includes("validateSession"));
  assert.ok(proxy.includes("export async function GET"));
  assert.ok(!proxy.includes("export async function POST"));
  assert.ok(backend.includes('method: "GET"'));
  assert.ok(backend.includes('"X-API-Key": await apiKey(role)'));
  assert.ok(backend.includes('cache: "no-store"'));
});

test("known missing backend contracts are documented", () => {
  const normalized = readme.replace(/\s+/g, " ");
  for (const boundary of ["Disabled historical universe rows", "Dedicated backtest job progress", "Unrealized P&L", "no audited browser mutation contract"]) assert.ok(normalized.includes(boundary));
});

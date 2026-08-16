"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type Market = "nse" | "forex" | "crypto";
type Page = "Overview" | "Decisions" | "Positions" | "Strategies" | "Features" | "Agent Runs" | "Outcomes" | "Operations";
type Session = { subject: string; role: "read" | "operator" | "admin" };
type Overview = { markets: Record<Market, { worker_state: string; last_heartbeat: string | null; provider_health: unknown; open_positions: number; outcomes: number }> };
type RecordValue = Record<string, unknown>;

const pages: Page[] = ["Overview", "Decisions", "Positions", "Strategies", "Features", "Agent Runs", "Outcomes", "Operations"];
const collectionPath: Partial<Record<Page, string>> = { Decisions: "decisions", Positions: "paper/positions", Strategies: "strategies", Features: "features", "Agent Runs": "agent-runs", Outcomes: "paper/outcomes" };

function format(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function timestamp(value: unknown): string {
  if (typeof value !== "string") return format(value);
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const data = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: data.get("username"), password: data.get("password") }) });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error ?? "Sign in failed");
      onLogin(body as Session);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Sign in failed"); }
    finally { setBusy(false); }
  }
  return <main className="login-shell"><form className="login-card" onSubmit={submit}><div className="logo">N</div><h1>NanoDelta</h1><p>Sign in to the paper-trading operations console.</p><label>Username<input name="username" autoComplete="username" required /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label>{error && <div className="state error">{error}</div>}<button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button><small>Credentials and backend API keys remain on the server.</small></form></main>;
}

function State({ kind, children }: { kind: "loading" | "error" | "empty" | "unavailable" | "stale"; children: React.ReactNode }) { return <div className={`state ${kind}`}>{children}</div>; }

function Records({ rows, query, side, timeframe, status }: { rows: RecordValue[]; query: string; side: string; timeframe: string; status: string }) {
  const filtered = useMemo(() => rows.filter((row) => {
    const symbol = format(row.symbol).toUpperCase();
    const rowSide = format(row.side ?? row.action ?? row.signal).toUpperCase();
    const rowTimeframe = format(row.timeframe ?? row.tf);
    const rowStatus = format(row.status ?? row.state);
    return (!query || symbol.includes(query.toUpperCase())) && (!side || rowSide === side) && (!timeframe || rowTimeframe === timeframe) && (!status || rowStatus === status);
  }), [rows, query, side, timeframe, status]);
  if (!rows.length) return <State kind="empty">No authoritative records are available for this market.</State>;
  if (!filtered.length) return <State kind="empty">No records match the selected filters.</State>;
  const preferred = ["occurred_at", "created_at", "event_time", "symbol", "side", "action", "signal", "timeframe", "stage", "status", "reason_code", "strategy_key"];
  const keys = [...preferred.filter((key) => filtered.some((row) => key in row)), ...Object.keys(filtered[0]).filter((key) => !preferred.includes(key))].slice(0, 9);
  return <div className="table-wrap"><table><thead><tr>{keys.map((key) => <th key={key}>{key.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{filtered.map((row, index) => <tr key={format(row.decision_id ?? row.position_id ?? row.record_id ?? index)}>{keys.map((key) => <td key={key} className={key.includes("time") || key.endsWith("_at") ? "mono muted" : ""}>{key.includes("time") || key.endsWith("_at") ? timestamp(row[key]) : format(row[key])}</td>)}</tr>)}</tbody></table></div>;
}

function Console({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [market, setMarket] = useState<Market>("nse"); const [page, setPage] = useState<Page>("Overview");
  const [data, setData] = useState<unknown>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState(""); const [loadedAt, setLoadedAt] = useState<Date | null>(null); const [stale, setStale] = useState(false);
  const [query, setQuery] = useState(""); const [side, setSide] = useState(""); const [timeframe, setTimeframe] = useState(""); const [status, setStatus] = useState("");
  const load = useCallback(async () => {
    setLoading(true); setError(""); setStale(false);
    const resource = page === "Overview" ? "overview" : page === "Operations" ? `${market}/health` : `${market}/${collectionPath[page]}`;
    try { const response = await fetch(`/api/backend/${resource}`, { cache: "no-store" }); const body = await response.json(); if (!response.ok) throw new Error(body.detail ?? body.error ?? `Request failed (${response.status})`); setData(body); setLoadedAt(new Date()); }
    catch (reason) { setData(null); setError(reason instanceof Error ? reason.message : "Backend unavailable"); }
    finally { setLoading(false); }
  }, [market, page]);
  useEffect(() => { queueMicrotask(() => void load()); }, [load]);
  useEffect(() => {
    if (!loadedAt) return;
    const timer = window.setTimeout(() => setStale(true), 60_000);
    return () => window.clearTimeout(timer);
  }, [loadedAt]);
  const rows = Array.isArray(data) ? data as RecordValue[] : [];
  const filterValues = (keys: string[]) => [...new Set(rows.map((row) => format(keys.map((name) => row[name]).find((value) => value !== undefined))).filter((value) => value !== "—"))].sort();
  return <div className="app"><aside className="sidebar"><div className="brand"><div className="logo">N</div><div><b>NanoDelta</b><small>AUTHORITATIVE CONSOLE</small></div></div><nav>{pages.map((item) => <button key={item} className={page === item ? "selected" : ""} onClick={() => setPage(item)}>◇ {item}</button>)}</nav><div className="sidebar-foot"><div><b>{session.subject}</b><small>{session.role.toUpperCase()} · secure session</small></div></div></aside><main className="main"><header><div className="market-switch">{(["nse", "forex", "crypto"] as Market[]).map((item) => <button key={item} className={market === item ? "active" : ""} onClick={() => setMarket(item)}>{item.toUpperCase()}</button>)}</div><div className="header-right"><span className="chip amber">PAPER ONLY</span><button className="secondary" onClick={() => void load()}>Refresh</button><button className="secondary" onClick={onLogout}>Sign out</button></div></header><div className="content"><div className="title-row"><div><span className="eyebrow">{market.toUpperCase()} · BACKEND API</span><h1>{page === "Decisions" ? "BUY/SELL Decisions" : page}</h1><p>{page === "Decisions" ? "Authoritative decision records, including rejected candidates and reason codes." : "Values shown below come from the NanoDelta backend API."}</p></div>{loadedAt && <div className="cycle"><div><small>LAST API RESPONSE</small><b>{loadedAt.toLocaleTimeString()}</b></div></div>}</div>{stale && <State kind="stale">Data may be stale. Refresh to request the latest backend state.</State>}{collectionPath[page] && <div className="filters"><label className="search"><input aria-label="Search symbol" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search symbol" /></label><select aria-label="Filter side" value={side} onChange={(event) => setSide(event.target.value)}><option value="">All BUY/SELL</option>{filterValues(["side", "action", "signal"]).map((value) => <option key={value}>{value}</option>)}</select><select aria-label="Filter timeframe" value={timeframe} onChange={(event) => setTimeframe(event.target.value)}><option value="">All timeframes</option>{filterValues(["timeframe", "tf"]).map((value) => <option key={value}>{value}</option>)}</select><select aria-label="Filter status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option>{filterValues(["status", "state"]).map((value) => <option key={value}>{value}</option>)}</select></div>}{loading ? <State kind="loading">Loading authoritative backend data…</State> : error ? <State kind="error"><b>Backend data unavailable.</b><br />{error}</State> : page === "Overview" ? <OverviewView value={data as Overview} /> : page === "Operations" ? <HealthView value={data as RecordValue} /> : <section className="panel"><div className="panel-head"><div><h2>{page === "Decisions" ? "Decision ledger" : `${page} records`}</h2><p>{rows.length} records returned by the backend</p></div></div><Records rows={rows} query={query} side={side} timeframe={timeframe} status={status} /></section>}</div></main></div>;
}

function OverviewView({ value }: { value: Overview }) {
  if (!value?.markets) return <State kind="unavailable">The backend returned no overview contract.</State>;
  return <div className="metrics">{Object.entries(value.markets).map(([market, state]) => <div className="metric" key={market}><p>{market.toUpperCase()}</p><strong>{format(state.worker_state)}</strong><small>Heartbeat: {timestamp(state.last_heartbeat)}</small><small>Open positions: {format(state.open_positions)} · Outcomes: {format(state.outcomes)}</small></div>)}</div>;
}

function HealthView({ value }: { value: RecordValue }) {
  if (!value) return <State kind="unavailable">Market health is not exposed by the backend.</State>;
  return <section className="panel"><div className="panel-head"><div><h2>{format(value.market).toUpperCase()} runtime health</h2><p>Direct response from /api/&#123;market&#125;/health</p></div></div><dl className="facts"><dt>Worker state</dt><dd>{format(value.worker_state)}</dd><dt>Last heartbeat</dt><dd>{timestamp(value.last_heartbeat)}</dd><dt>Providers</dt><dd><pre>{JSON.stringify(value.providers ?? {}, null, 2)}</pre></dd></dl></section>;
}

export default function Home() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  useEffect(() => { fetch("/api/auth/session", { cache: "no-store" }).then(async (response) => setSession(response.ok ? await response.json() : null)).catch(() => setSession(null)); }, []);
  if (session === undefined) return <main className="login-shell"><State kind="loading">Checking secure session…</State></main>;
  if (!session) return <Login onLogin={setSession} />;
  return <Console session={session} onLogout={async () => { await fetch("/api/auth/logout", { method: "POST" }); setSession(null); }} />;
}

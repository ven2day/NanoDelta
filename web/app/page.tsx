"use client";

import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";

type Role = "viewer" | "operator" | "admin";
type Session = { subject: string; role: Role };
type Json = Record<string, unknown>;
type ApiPage = { items: Json[]; page?: { total?: number }; freshness?: { freshest_at?: string | null } };
type SessionStatus = { state?: string; reason?: string; holiday_calendar_complete?: boolean };
type Health = { market?: string; worker_state?: string; last_heartbeat?: string | null; providers?: Json; session?: SessionStatus };
type View = "Workspace" | "Dashboard" | "Universe" | "Strategies" | "Signals" | "Decisions" | "Positions" | "Risk" | "Backtests" | "Reports" | "Logs" | "Settings";
type WorkspaceData = { health: Health; decisions: ApiPage; signals: ApiPage; universe: ApiPage; strategies: ApiPage; features: ApiPage; orders: ApiPage; positions: ApiPage };
type WorkspaceRow = {
  key: string; candidateId: string | null; cycleId: string; symbol: string; timeframe: string;
  strategyKey: string; strategy: string; signal: string; expectedR: number | null;
  decision: "ACCEPT" | "REJECT" | "PENDING"; reason: string; data: "READY" | "PARTIAL" | "NOT READY";
  events: Json[]; order?: Json; candidate?: Json;
};
type FilterKey = "symbol" | "timeframe" | "strategy" | "action" | "decision" | "status" | "provider" | "freshness" | "date" | "cycle";
type Filters = Record<FilterKey, string>;
type OperationalData = {
  primary?: ApiPage; secondary?: ApiPage; tertiary?: ApiPage; metrics?: Json;
  health?: Health; session?: SessionStatus; overview?: Json;
};

const emptyFilters: Filters = { symbol: "", timeframe: "", strategy: "", action: "", decision: "", status: "", provider: "", freshness: "", date: "", cycle: "" };
const filterKeys = Object.keys(emptyFilters) as FilterKey[];

const views: { name: View; icon: string }[] = [
  { name: "Dashboard", icon: "⌂" }, { name: "Universe", icon: "◎" },
  { name: "Strategies", icon: "⌘" }, { name: "Signals", icon: "⌁" },
  { name: "Decisions", icon: "▣" }, { name: "Positions", icon: "▤" },
  { name: "Risk", icon: "◇" }, { name: "Backtests", icon: "▧" },
  { name: "Reports", icon: "□" }, { name: "Logs", icon: "≡" },
  { name: "Settings", icon: "⚙" },
];

const stageOrder = ["data_readiness", "tradeability", "strategy_eligibility", "signal", "scoring", "llm_review", "portfolio_construction", "entry_revalidation", "risk", "execution"];

function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function number(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function time(value: unknown): string {
  if (typeof value !== "string") return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dateTime(value: unknown): string {
  if (typeof value !== "string") return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function pretty(value: unknown): string {
  return text(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function timestamp(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

function freshnessOf(value: unknown): "fresh" | "stale" | "unavailable" {
  const parsed = timestamp(value);
  if (parsed === null) return "unavailable";
  return Date.now() - parsed <= 15 * 60_000 ? "fresh" : "stale";
}

function matchesFreshness(value: unknown, filter: string): boolean {
  return !filter || freshnessOf(value) === filter;
}

function matchesDate(value: unknown, filter: string): boolean {
  if (!filter) return true;
  const parsed = timestamp(value);
  if (parsed === null) return false;
  const durations: Record<string, number> = { "1h": 60 * 60_000, "24h": 24 * 60 * 60_000, "7d": 7 * 24 * 60 * 60_000 };
  return Date.now() - parsed <= (durations[filter] ?? Infinity);
}

async function backend<T>(path: string): Promise<T> {
  const response = await fetch(`/api/backend/${path}`, { cache: "no-store" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(text(body.detail ?? body.error, `Request failed (${response.status})`));
  return body as T;
}

function Logo() {
  return <span className="brand-mark" aria-hidden="true"><i /><b /></span>;
}

function Login({ onLogin }: { onLogin: (session: Session) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: form.get("username"), password: form.get("password") }) });
      const body = await response.json();
      if (!response.ok) throw new Error(text(body.error, "Sign in failed"));
      onLogin(body as Session);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Sign in failed"); }
    finally { setBusy(false); }
  }
  return <main className="login-shell"><form className="login-card" onSubmit={submit}>
    <div className="login-brand"><Logo /><strong>NanoDelta</strong></div>
    <h1>NSE Research Workspace</h1><p>Sign in to the authoritative paper-trading console.</p>
    <label>Username<input name="username" autoComplete="username" required /></label>
    <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
    {error && <State kind="error">{error}</State>}
    <button className="primary-button" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
    <small>Credentials and backend keys remain server-side.</small>
  </form></main>;
}

function State({ kind, children }: { kind: "loading" | "error" | "empty" | "warning" | "unavailable" | "stale"; children: ReactNode }) {
  return <div className={`state state-${kind}`}>{children}</div>;
}

function useUrlFilters(namespace: View): [Filters, (key: FilterKey, value: string) => void, () => void] {
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  useEffect(() => {
    const read = () => {
      const search = new URLSearchParams(window.location.search);
      const next = { ...emptyFilters };
      for (const key of filterKeys) next[key] = search.get(key) ?? "";
      setFilters(next);
    };
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [namespace]);
  const setFilter = useCallback((key: FilterKey, value: string) => {
    setFilters((current) => ({ ...current, [key]: value }));
    const url = new URL(window.location.href);
    url.searchParams.set("view", namespace);
    if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    window.history.replaceState({}, "", url);
  }, [namespace]);
  const clear = useCallback(() => {
    setFilters(emptyFilters);
    const url = new URL(window.location.href);
    for (const key of filterKeys) url.searchParams.delete(key);
    url.searchParams.set("view", namespace);
    window.history.replaceState({}, "", url);
  }, [namespace]);
  return [filters, setFilter, clear];
}

function Shell({ session, view, setView, onLogout, children, health, freshestAt }: { session: Session; view: View; setView: (view: View) => void; onLogout: () => void; children: ReactNode; health?: Health; freshestAt?: string | null }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const provider = health?.providers ?? {};
  const feedState = text(provider.state, "UNAVAILABLE");
  const activeProvider = text(provider.active_provider, "No feed");
  const running = text(health?.worker_state, "UNKNOWN");
  const exchange = text(health?.session?.state, "UNKNOWN");
  const fresh = Boolean(freshestAt);
  return <div className="app-shell">
    <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
      <div className="brand"><Logo /><span><strong>NanoDelta</strong><small>QUANT RESEARCH</small></span></div>
      <button className="workspace-link selected" onClick={() => { setView("Workspace"); setMobileOpen(false); }}><span>▥</span>NSE Workspace</button>
      <nav>{views.map((item) => <button key={item.name} className={(view === item.name || (view === "Workspace" && item.name === "Decisions")) ? "active" : ""} onClick={() => { setView(item.name); setMobileOpen(false); }}><span>{item.icon}</span>{item.name}</button>)}</nav>
      <div className="user-card"><span className="avatar">{session.subject.slice(0, 2).toUpperCase()}</span><div><strong>{session.subject}</strong><small>{session.role.toUpperCase()} · secure session</small></div><button aria-label="Sign out" onClick={onLogout}>⌄</button></div>
    </aside>
    <main className="workspace-main">
      <header className="topbar"><button className="menu-button" aria-label="Open navigation" aria-expanded={mobileOpen} onClick={() => setMobileOpen((open) => !open)}>☰</button>
        <div className="market-tabs"><button className="active">NSE</button><button disabled>FOREX</button><button disabled>CRYPTO</button></div>
        <strong className="workspace-title">NSE Workspace</strong>
        <div className="status-strip">
          <StatusChip label="NSE" value={exchange} good={exchange === "OPEN"} />
          <StatusChip label="Runtime" value={running} good={running === "RUNNING"} />
          <StatusChip label={activeProvider} value={feedState} good={feedState === "HEALTHY" || feedState === "FAILED_OVER"} />
          <StatusChip label="Data API" value={fresh ? "AVAILABLE" : "EMPTY"} good={fresh} />
          <span className="paper-mode">▤&nbsp; Paper Mode</span>
        </div>
      </header>
      {children}
    </main>
    {mobileOpen && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}
  </div>;
}

function StatusChip({ label, value, good }: { label: string; value: string; good: boolean }) {
  return <span className="status-chip"><b>{label}</b><i className={good ? "good" : "warn"} />{value}</span>;
}

function metric(events: Json[], name: string): number | null {
  for (const event of events) {
    const metrics = event.metrics;
    if (metrics && typeof metrics === "object" && name in metrics) return number((metrics as Json)[name]);
  }
  return null;
}

function buildRows(data: WorkspaceData, requestedCycle = ""): { rows: WorkspaceRow[]; cycleId: string; cycleAt: string | null } {
  const all = data.decisions.items;
  const latest = [...all].sort((a, b) => text(b.occurred_at).localeCompare(text(a.occurred_at)))[0];
  const cycleId = requestedCycle || text(latest?.cycle_id, "");
  const cycleAnchor = all.find((event) => text(event.cycle_id, "") === cycleId);
  const cycleAt = typeof cycleAnchor?.occurred_at === "string" ? cycleAnchor.occurred_at : null;
  const cycle = all.filter((event) => text(event.cycle_id, "") === cycleId);
  const groups = new Map<string, Json[]>();
  for (const event of cycle) {
    if (event.stage !== "signal" && !event.candidate_id) continue;
    const key = text(event.candidate_id, `${text(event.symbol)}:${text(event.strategy_key)}:${text(event.timeframe)}`);
    groups.set(key, [...(groups.get(key) ?? []), event]);
  }
  const orders = data.orders.items;
  return {
    cycleId, cycleAt,
    rows: [...groups.entries()].map(([key, candidateEvents]): WorkspaceRow => {
      const first = candidateEvents[0];
      const candidateId = typeof first.candidate_id === "string" ? first.candidate_id : null;
      const related = cycle.filter((event) => event.symbol === first.symbol && (!candidateId || event.candidate_id === candidateId || !event.candidate_id));
      const sorted = [...related].sort((a, b) => stageOrder.indexOf(text(a.stage)) - stageOrder.indexOf(text(b.stage)));
      const terminal = [...candidateEvents].sort((a, b) => stageOrder.indexOf(text(b.stage)) - stageOrder.indexOf(text(a.stage)))[0];
      const execution = candidateEvents.find((event) => event.stage === "execution" && event.status === "ordered");
      const rejected = [...candidateEvents].reverse().find((event) => event.status === "rejected");
      const order = orders.find((item) => candidateId && item.candidate_id === candidateId);
      const candidate = data.signals.items.find((item) => candidateId && item.candidate_id === candidateId);
      const readinessEvent = related.find((event) => event.stage === "data_readiness");
      const strategyKey = text(first.strategy_key, "");
      const strategyRecord = data.strategies.items.find((item) => item.strategy_key === strategyKey);
      return {
        key, candidateId, cycleId, symbol: text(first.symbol), timeframe: text(first.timeframe), strategyKey,
        strategy: text(strategyRecord?.strategy_id, strategyKey ? strategyKey.split(":").at(-1) : "No trigger"),
        signal: text(candidate?.action ?? order?.action, terminal?.reason_code === "NO_TRIGGER" ? "ABSTAIN" : "—"),
        expectedR: metric(candidateEvents, "expected_r_net_of_costs"),
        decision: execution ? "ACCEPT" : rejected ? "REJECT" : "PENDING",
        reason: text((execution ?? rejected ?? terminal)?.reason_code),
        data: readinessEvent?.status === "passed" ? "READY" : readinessEvent ? "NOT READY" : "PARTIAL",
        events: sorted, order, candidate,
      };
    }).sort((a, b) => (b.expectedR ?? -Infinity) - (a.expectedR ?? -Infinity)),
  };
}

function SummaryCard({ icon, label, value, note, tone = "green" }: { icon: string; label: string; value: number | string; note: string; tone?: "green" | "blue" | "amber" }) {
  return <article className="summary-card"><span className="summary-icon">{icon}</span><div><small>{label}</small><strong>{value}</strong><p>{note}</p></div><span className={`summary-check ${tone}`}>✓</span></article>;
}

function Workspace({ onSnapshot, namespace }: { onSnapshot: (health: Health, freshest: string | null) => void; namespace: View }) {
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadedAt, setLoadedAt] = useState(0);
  const [selectedKey, setSelectedKey] = useState("");
  const [filters, setFilter, clearFilters] = useUrlFilters(namespace);
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true); setError("");
    try {
      const [health, session, decisions, signals, universe, strategies, features, orders, positions] = await Promise.all([
        backend<Health>("nse/health"), backend<SessionStatus>("nse/session"), backend<ApiPage>("nse/decision-events?limit=500"),
        backend<ApiPage>("nse/signals?limit=500"), backend<ApiPage>("nse/universe?enabled=true&limit=1000"),
        backend<ApiPage>("nse/strategy-validation/strategies?limit=500"), backend<ApiPage>("nse/features?limit=500"),
        backend<ApiPage>("nse/orders?limit=500"), backend<ApiPage>("nse/positions?limit=500"),
      ]);
      const currentHealth = { ...health, session };
      const next = { health: currentHealth, decisions, signals, universe, strategies, features, orders, positions };
      setData(next); setLoadedAt(Date.now()); onSnapshot(currentHealth, features.freshness?.freshest_at ?? null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "NSE workspace unavailable"); }
    finally { setLoading(false); }
  }, [onSnapshot]);
  useEffect(() => { queueMicrotask(() => void load()); const timer = window.setInterval(() => { if (!document.hidden) void load(true); }, 10_000); return () => window.clearInterval(timer); }, [load]);
  const built = useMemo(() => data ? buildRows(data, filters.cycle) : { rows: [], cycleId: "", cycleAt: null }, [data, filters.cycle]);
  const filterValues = (field: "signal" | "timeframe" | "strategy" | "data") => [...new Set(built.rows.map((row) => text(row[field])).filter((value) => value !== "—"))].sort();
  const filtered = built.rows.filter((row) => (!filters.symbol || row.symbol.toLowerCase().includes(filters.symbol.toLowerCase())) && (!filters.action || row.signal === filters.action) && (!filters.decision || row.decision === filters.decision) && (!filters.timeframe || row.timeframe === filters.timeframe) && (!filters.strategy || row.strategy === filters.strategy) && (!filters.status || row.data === filters.status) && matchesDate(row.events.at(-1)?.occurred_at, filters.date) && matchesFreshness(row.events.at(-1)?.occurred_at, filters.freshness));
  const selected = built.rows.find((row) => row.key === selectedKey) ?? filtered[0];
  if (loading) return <div className="workspace-content"><State kind="loading">Loading authoritative NSE workspace…</State></div>;
  if (error) return <div className="workspace-content"><State kind="error"><b>NSE workspace unavailable.</b><br />{error}<br /><button onClick={() => void load()}>Retry</button></State></div>;
  if (!data) return null;
  const latestCycleEvents = data.decisions.items.filter((event) => event.cycle_id === built.cycleId);
  const observedUniverse = data.universe.page?.total ?? data.universe.items.length;
  const ready = new Set(latestCycleEvents.filter((item) => item.stage === "data_readiness" && item.status === "passed").map((item) => text(item.symbol))).size;
  const eligible = data.strategies.items.filter((item) => item.approval_state === "APPROVED" && (!item.expires_at || new Date(text(item.expires_at)).getTime() > loadedAt)).length;
  const final = built.rows.filter((row) => row.decision === "ACCEPT").length;
  return <div className="workspace-content">
    <section className="summary-grid">
      <SummaryCard icon="◎" label="Configured Universe" value={observedUniverse} note="Durable enabled NSE symbols" />
      <SummaryCard icon="▱" label="Data Ready" value={ready} note="Latest decision cycle" />
      <SummaryCard icon="⌘" label="Eligible Strategies" value={eligible} note="Approved and unexpired" />
      <SummaryCard icon="▽" label="Candidates" value={built.rows.filter((row) => row.candidateId).length} note="Latest cycle" tone="blue" />
      <SummaryCard icon="▣" label="Final Decisions" value={final} note="Paper orders created" tone="blue" />
    </section>
    <section className="decision-grid">
      <div className="left-stack">
        <div className="panel candidates-panel">
          <div className="filters">
            <input aria-label="Search symbol" placeholder="Search symbol" value={filters.symbol} onChange={(e) => setFilter("symbol", e.target.value)} />
            <Filter value={filters.timeframe} onChange={(value) => setFilter("timeframe", value)} label="All TF" options={filterValues("timeframe")} />
            <Filter value={filters.strategy} onChange={(value) => setFilter("strategy", value)} label="All strategies" options={filterValues("strategy")} />
            <Filter value={filters.action} onChange={(value) => setFilter("action", value)} label="BUY / SELL" options={filterValues("signal")} />
            <Filter value={filters.decision} onChange={(value) => setFilter("decision", value)} label="All decisions" options={["ACCEPT", "REJECT", "PENDING"]} />
            <Filter value={filters.status} onChange={(value) => setFilter("status", value)} label="All data" options={["READY", "PARTIAL", "NOT READY"]} />
            <Filter value={filters.date} onChange={(value) => setFilter("date", value)} label="Any date" options={["1h", "24h", "7d"]} />
            <Filter value={filters.freshness} onChange={(value) => setFilter("freshness", value)} label="Any freshness" options={["fresh", "stale", "unavailable"]} />
            <input aria-label="Decision cycle" placeholder="Cycle ID" value={filters.cycle} onChange={(e) => setFilter("cycle", e.target.value)} />
            {filterKeys.some((key) => filters[key]) && <button className="filter-clear" onClick={clearFilters}>Clear</button>}
            <span>{filtered.length} rows</span>
          </div>
          <div className="table-scroll"><table className="candidate-table"><thead><tr><th>Symbol</th><th>Data</th><th>Stage</th><th>Strategy</th><th>Signal</th><th>Expected R</th><th>Decision</th><th>Reason</th></tr></thead>
            <tbody>{filtered.map((row) => <tr key={row.key} className={selected?.key === row.key ? "selected-row" : ""} onClick={() => setSelectedKey(row.key)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") setSelectedKey(row.key); }}>
              <td className="symbol-cell">☆ <b>{row.symbol}</b></td><td><DotState value={row.data} /></td><td>{pretty(row.events.at(-1)?.stage)}</td><td>{pretty(row.strategy)}</td><td><Badge value={row.signal} /></td><td>{row.expectedR === null ? "—" : row.expectedR.toFixed(2)}</td><td><Badge value={row.decision} /></td><td className="reason-cell">{pretty(row.reason)}</td>
            </tr>)}</tbody></table></div>
          {!filtered.length && <State kind="empty">No authoritative candidates match these filters.</State>}
          <div className="legend"><span><i className="ready" />Ready</span><span><i className="partial" />Partial</span><span><i className="not-ready" />Not ready</span><b>Cycle {built.cycleId ? built.cycleId.slice(0, 10) : "—"} · {time(built.cycleAt)}</b></div>
        </div>
        <PriceChart row={selected} />
      </div>
      <aside className="right-stack"><Lifecycle row={selected} /><Attribution row={selected} /></aside>
    </section>
  </div>;
}

function Filter({ value, onChange, label, options }: { value: string; onChange: (value: string) => void; label: string; options: string[] }) {
  return <select value={value} onChange={(event) => onChange(event.target.value)}><option value="">{label}</option>{options.map((option) => <option key={option}>{option}</option>)}</select>;
}

function DotState({ value }: { value: string }) { return <span className="dot-state"><i className={value.toLowerCase().replace(" ", "-")} />{pretty(value)}</span>; }

function Badge({ value }: { value: string }) { return <span className={`badge badge-${value.toLowerCase().replaceAll(" ", "-")}`}>{value}</span>; }

function Lifecycle({ row }: { row?: WorkspaceRow }) {
  return <section className="panel lifecycle"><h2>Decision Lifecycle: <span>{row?.symbol ?? "—"}</span></h2>
    {!row ? <State kind="empty">Select a candidate to inspect its lifecycle.</State> : <ol>{row.events.map((event) => <li key={text(event.decision_id)} className={text(event.status)}><i>{event.status === "rejected" ? "×" : event.status === "ordered" ? "▣" : "✓"}</i><div><strong>{pretty(event.stage)}</strong><p>{pretty(event.reason_code)}</p></div><span><b>{text(event.status).toUpperCase()}</b><small>{time(event.occurred_at)}</small></span></li>)}</ol>}
  </section>;
}

function Attribution({ row }: { row?: WorkspaceRow }) {
  const values = [
    ["Regime Fit", metric(row?.events ?? [], "market_regime_fit")], ["Sector Strength", metric(row?.events ?? [], "sector_regime_fit")],
    ["MTF Alignment", metric(row?.events ?? [], "mtf_alignment")], ["Costs & Slippage", metric(row?.events ?? [], "estimated_cost_r")],
    ["Strategy Confidence", metric(row?.events ?? [], "strategy_confidence")],
  ] as const;
  return <section className="panel attribution"><h2>Decision Attribution: <span>{row?.symbol ?? "—"}</span></h2>
    <div className="attribution-list">{values.map(([label, value]) => <div key={label}><span>▤&nbsp; {label}</span><small>{value === null ? "Not persisted" : value.toFixed(2)}</small><b className={value === null ? "muted" : "positive"}>{value === null ? "—" : "Available"}</b></div>)}</div>
    <div className="decision-result"><div><small>Expected R</small><strong>{row?.expectedR === null || row?.expectedR === undefined ? "—" : row.expectedR.toFixed(2)}</strong></div><div><small>Decision</small><strong className={row?.decision === "ACCEPT" ? "positive" : row?.decision === "REJECT" ? "negative" : "pending"}>{row?.decision ?? "—"}</strong></div><div><small>Position Bias</small><strong className={row?.signal === "BUY" ? "positive" : row?.signal === "SELL" ? "negative" : "muted"}>{row?.signal ?? "—"}</strong></div></div>
  </section>;
}

function PriceChart({ row }: { row?: WorkspaceRow }) {
  const [candles, setCandles] = useState<Json[]>([]); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  useEffect(() => {
    if (!row?.symbol || !row.timeframe || row.timeframe === "—") { queueMicrotask(() => setCandles([])); return; }
    let cancelled = false; queueMicrotask(() => { setLoading(true); setError(""); });
    backend<ApiPage>(`nse/candles?symbol=${encodeURIComponent(row.symbol)}&timeframe=${encodeURIComponent(row.timeframe)}&limit=160`).then((page) => { if (!cancelled) setCandles(page.items); }).catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Chart unavailable"); }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [row?.symbol, row?.timeframe]);
  const order = row?.order; const candidate = row?.candidate;
  const entry = number(order?.fill_price ?? order?.reference_price ?? candidate?.reference_price);
  const stop = number(order?.stop_price ?? candidate?.stop_price);
  const target = number(order?.target_price ?? candidate?.target_price);
  return <section className="panel chart-panel"><div className="chart-head"><strong>{row?.symbol ?? "Select a symbol"} <i /> {row?.timeframe ?? "—"}</strong><span><b className="line-blue" />Close</span><span><b className="line-green" />Entry {entry?.toLocaleString() ?? "—"}</span><span><b className="line-red" />Stop {stop?.toLocaleString() ?? "—"}</span><span><b className="line-target" />Target {target?.toLocaleString() ?? "—"}</span></div>
    {loading ? <State kind="loading">Loading settled candles…</State> : error ? <State kind="warning">{error}</State> : candles.length ? <CandleSvg candles={candles} entry={entry} stop={stop} target={target} /> : <State kind="empty">No settled candles are available for this symbol and timeframe.</State>}
    <footer>Source: authoritative Silver candles <span>Candidate levels are persisted; paper fills supersede proposed entry.</span></footer></section>;
}

function CandleSvg({ candles, entry, stop, target }: { candles: Json[]; entry: number | null; stop: number | null; target: number | null }) {
  const rows = [...candles].sort((a, b) => text(a.open_time).localeCompare(text(b.open_time))).slice(-90);
  const values = rows.flatMap((item) => [number(item.low), number(item.high)]).filter((item): item is number => item !== null);
  for (const mark of [entry, stop, target]) if (mark !== null) values.push(mark);
  const minimum = Math.min(...values); const maximum = Math.max(...values); const range = maximum - minimum || 1;
  const width = 900; const height = 310; const top = 18; const bottom = 26; const chartHeight = height - top - bottom; const step = width / Math.max(rows.length, 1);
  const y = (value: number) => top + (maximum - value) / range * chartHeight;
  const marks: [number | null, string, string][] = [[target, "Target", "#44d58a"], [entry, "Entry", "#5ad798"], [stop, "Stop", "#ff625d"]];
  return <div className="chart-wrap"><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Authoritative settled candlestick chart">
    {[0, 1, 2, 3, 4].map((line) => { const lineY = top + chartHeight / 4 * line; const price = maximum - range / 4 * line; return <g key={line}><line x1="0" x2={width} y1={lineY} y2={lineY} className="grid-line" /><text x={width - 4} y={lineY - 4} textAnchor="end" className="axis-label">{price.toFixed(2)}</text></g>; })}
    {rows.map((item, index) => { const open = number(item.open) ?? 0; const close = number(item.close) ?? 0; const high = number(item.high) ?? 0; const low = number(item.low) ?? 0; const x = index * step + step / 2; const up = close >= open; return <g key={text(item.open_time, String(index))} className={up ? "candle-up" : "candle-down"}><line x1={x} x2={x} y1={y(high)} y2={y(low)} /><rect x={x - Math.max(1.5, step * .28)} width={Math.max(3, step * .56)} y={Math.min(y(open), y(close))} height={Math.max(1.5, Math.abs(y(open) - y(close)))} /></g>; })}
    {marks.map(([value, label, color]) => value === null ? null : <g key={label}><line x1="0" x2={width} y1={y(value)} y2={y(value)} stroke={color} strokeDasharray="7 6" /><text x={width - 8} y={y(value) - 7} textAnchor="end" fill={color} className="mark-label">{label} {value.toFixed(2)}</text></g>)}
  </svg></div>;
}

const operationalCopy: Record<Exclude<View, "Workspace" | "Decisions">, { title: string; description: string }> = {
  Dashboard: { title: "NSE Command Center", description: "Runtime, market session, exposure and recent paper-trading activity from authoritative services." },
  Universe: { title: "Configured Universe", description: "The durable symbol set published by the NSE runtime configuration." },
  Strategies: { title: "Strategy Governance", description: "Registered strategy definitions, latest approval state and validation evidence." },
  Signals: { title: "BUY / SELL Signals", description: "Immutable candidates emitted by approved strategies before portfolio and risk decisions." },
  Positions: { title: "Paper Positions", description: "Durable paper positions and their originating order activity." },
  Risk: { title: "Risk Monitor", description: "Authoritative open exposure, realized results and persisted risk-stage decisions." },
  Backtests: { title: "Validation & Backtests", description: "Persisted validation runs used by strategy governance." },
  Reports: { title: "Performance & Reports", description: "Closed-trade performance and durable report-generation records." },
  Logs: { title: "Operations Log", description: "Immutable operational audit commands and production alert records." },
  Settings: { title: "NSE Settings", description: "Effective durable settings and current runtime configuration state." },
};

async function loadOperational(view: Exclude<View, "Workspace" | "Decisions">): Promise<OperationalData> {
  switch (view) {
    case "Dashboard": {
      const [overview, health, session, universe, signals, positions, performance, risk, alerts] = await Promise.all([
        backend<Json>("overview"), backend<Health>("nse/health"), backend<SessionStatus>("nse/session"), backend<ApiPage>("nse/universe?enabled=true&limit=1000"),
        backend<ApiPage>("nse/signals?limit=100"), backend<ApiPage>("nse/positions?limit=100"), backend<Json>("nse/performance"), backend<Json>("nse/risk/aggregate"), backend<ApiPage>("alerts?market=nse&limit=100"),
      ]);
      return { overview, health, session, primary: signals, secondary: positions, tertiary: alerts, metrics: { universe_total: universe.page?.total ?? universe.items.length, performance, risk } };
    }
    case "Universe": return { primary: await backend<ApiPage>("nse/universe?enabled=true&limit=1000") };
    case "Strategies": { const [primary, secondary] = await Promise.all([backend<ApiPage>("nse/strategy-validation/strategies?limit=500"), backend<ApiPage>("nse/strategy-validation/backtests?limit=500")]); return { primary, secondary }; }
    case "Signals": return { primary: await backend<ApiPage>("nse/signals?limit=500") };
    case "Positions": { const [primary, secondary] = await Promise.all([backend<ApiPage>("nse/positions?limit=500"), backend<ApiPage>("nse/orders?limit=500")]); return { primary, secondary }; }
    case "Risk": { const [metrics, primary, secondary] = await Promise.all([backend<Json>("nse/risk/aggregate"), backend<ApiPage>("nse/positions?state=OPEN&limit=500"), backend<ApiPage>("nse/decision-events?stage=risk&limit=500")]); return { metrics, primary, secondary }; }
    case "Backtests": return { primary: await backend<ApiPage>("nse/strategy-validation/backtests?limit=500") };
    case "Reports": { const [primary, metrics, secondary] = await Promise.all([backend<ApiPage>("reports?market=nse&limit=500"), backend<Json>("nse/performance"), backend<ApiPage>("nse/trades?limit=500")]); return { primary, metrics, secondary }; }
    case "Logs": { const [primary, secondary] = await Promise.all([backend<ApiPage>("audit?market=nse&limit=500"), backend<ApiPage>("alerts?market=nse&limit=500")]); return { primary, secondary }; }
    case "Settings": { const [primary, health, session] = await Promise.all([backend<ApiPage>("settings?market=nse&limit=500"), backend<Health>("nse/health"), backend<SessionStatus>("nse/session")]); return { primary, health, session }; }
  }
}

function recordTime(row: Json): unknown {
  for (const key of ["event_time", "occurred_at", "updated_at", "evaluated_at", "requested_at", "started_at", "configured_at", "created_at", "opened_at"]) if (row[key]) return row[key];
  return null;
}

function rowStatus(row: Json): string { return text(row.state ?? row.status ?? row.approval_state ?? row.resulting_state ?? row.passed, ""); }

function filterRows(rows: Json[], filters: Filters): Json[] {
  return rows.filter((row) => {
    const contains = (value: unknown, query: string) => !query || text(value, "").toLowerCase().includes(query.toLowerCase());
    return contains(row.symbol, filters.symbol)
      && (!filters.timeframe || text(row.timeframe, "") === filters.timeframe)
      && contains(row.strategy_key ?? row.strategy_id, filters.strategy)
      && (!filters.action || text(row.action, "") === filters.action)
      && (!filters.decision || text(row.status, "") === filters.decision)
      && (!filters.status || rowStatus(row) === filters.status)
      && (!filters.provider || text(row.provider, "") === filters.provider)
      && contains(row.cycle_id, filters.cycle)
      && matchesDate(recordTime(row), filters.date)
      && matchesFreshness(recordTime(row), filters.freshness);
  });
}

function pageFreshness(data: OperationalData): string | null {
  const candidates = [data.primary?.freshness?.freshest_at, data.secondary?.freshness?.freshest_at, data.tertiary?.freshness?.freshest_at, (data.metrics?.freshness as Json | undefined)?.freshest_at]
    .filter((value): value is string => typeof value === "string");
  return candidates.sort().at(-1) ?? null;
}

function Freshness({ value }: { value?: string | null }) {
  const state = freshnessOf(value);
  return <span className={`freshness freshness-${state}`}><i />{state === "unavailable" ? "Freshness unavailable" : `${pretty(state)} · ${dateTime(value)}`}</span>;
}

function CapabilityNotice({ session, capability }: { session: Session; capability: string }) {
  const privileged = session.role === "operator" || session.role === "admin";
  return <div className="capability-notice"><span>{privileged ? `${pretty(session.role)} access` : "Viewer access"}</span><p>{privileged ? `${capability} is unavailable because no audited backend mutation contract exists.` : `${capability} requires operator or admin access and an audited backend mutation contract.`}</p><button disabled aria-disabled="true">Unavailable</button></div>;
}

function filterOptions(rows: Json[], key: FilterKey): string[] {
  const values = rows.map((row) => {
    if (key === "strategy") return row.strategy_key ?? row.strategy_id;
    if (key === "status") return rowStatus(row);
    if (key === "cycle") return row.cycle_id;
    return row[key];
  }).map((value) => text(value, "")).filter(Boolean);
  return [...new Set(values)].sort();
}

function OperationalFilters({ view, rows, filters, setFilter, clear }: { view: View; rows: Json[]; filters: Filters; setFilter: (key: FilterKey, value: string) => void; clear: () => void }) {
  const fields: Partial<Record<View, FilterKey[]>> = {
    Universe: ["symbol", "provider", "freshness"], Strategies: ["timeframe", "strategy", "status", "date", "freshness"],
    Signals: ["symbol", "timeframe", "strategy", "action", "cycle", "date", "freshness"], Positions: ["symbol", "status", "date", "freshness"],
    Risk: ["symbol", "status", "date", "freshness"], Backtests: ["strategy", "status", "date", "freshness"],
    Reports: ["status", "date", "freshness"], Logs: ["status", "date", "freshness"], Settings: ["freshness"],
  };
  const active = fields[view] ?? [];
  if (!active.length) return null;
  return <div className="page-filters" aria-label={`${view} filters`}>
    {active.includes("symbol") && <input aria-label="Filter symbol" placeholder="Symbol" value={filters.symbol} onChange={(event) => setFilter("symbol", event.target.value)} />}
    {active.filter((key) => !["symbol", "date", "freshness"].includes(key)).map((key) => <Filter key={key} value={filters[key]} onChange={(value) => setFilter(key, value)} label={`All ${key}`} options={filterOptions(rows, key)} />)}
    {active.includes("date") && <Filter value={filters.date} onChange={(value) => setFilter("date", value)} label="Any date" options={["1h", "24h", "7d"]} />}
    {active.includes("freshness") && <Filter value={filters.freshness} onChange={(value) => setFilter("freshness", value)} label="Any freshness" options={["fresh", "stale", "unavailable"]} />}
    {filterKeys.some((key) => filters[key]) && <button className="filter-clear" onClick={clear}>Clear filters</button>}
  </div>;
}

type Column = { key: string; label?: string; kind?: "time" | "badge" | "number" };
const columns: Partial<Record<View, Column[]>> = {
  Universe: [{ key: "symbol" }, { key: "provider" }, { key: "provider_symbol" }, { key: "timeframes" }, { key: "trade_horizon" }, { key: "enabled", kind: "badge" }, { key: "configured_at", kind: "time" }],
  Strategies: [{ key: "strategy_id" }, { key: "strategy_version" }, { key: "timeframe" }, { key: "family" }, { key: "approval_state", kind: "badge" }, { key: "expires_at", kind: "time" }, { key: "implementation_ref" }],
  Signals: [{ key: "event_time", kind: "time" }, { key: "symbol" }, { key: "timeframe" }, { key: "strategy_key" }, { key: "action", kind: "badge" }, { key: "reference_price", kind: "number" }, { key: "stop_price", kind: "number" }, { key: "target_price", kind: "number" }, { key: "confidence", kind: "number" }, { key: "cycle_id" }],
  Positions: [{ key: "symbol" }, { key: "state", kind: "badge" }, { key: "signed_quantity", kind: "number" }, { key: "average_entry_price", kind: "number" }, { key: "realized_pnl", kind: "number" }, { key: "total_fees", kind: "number" }, { key: "strategy_keys" }, { key: "updated_at", kind: "time" }],
  Risk: [{ key: "symbol" }, { key: "state", kind: "badge" }, { key: "signed_quantity", kind: "number" }, { key: "average_entry_price", kind: "number" }, { key: "realized_pnl", kind: "number" }, { key: "total_fees", kind: "number" }, { key: "updated_at", kind: "time" }],
  Backtests: [{ key: "evaluated_at", kind: "time" }, { key: "strategy_key" }, { key: "passed", kind: "badge" }, { key: "rejection_reasons" }, { key: "metrics" }, { key: "policy" }],
  Reports: [{ key: "started_at", kind: "time" }, { key: "report_type" }, { key: "state", kind: "badge" }, { key: "completed_at", kind: "time" }, { key: "requested_by" }, { key: "artifact_uri" }],
  Logs: [{ key: "requested_at", kind: "time" }, { key: "command" }, { key: "actor_id" }, { key: "previous_state" }, { key: "resulting_state", kind: "badge" }, { key: "detail" }],
  Settings: [{ key: "setting_key" }, { key: "value" }, { key: "updated_at", kind: "time" }, { key: "updated_by" }],
};
const alertColumns: Column[] = [{ key: "occurred_at", kind: "time" }, { key: "severity", kind: "badge" }, { key: "component" }, { key: "reason_code" }, { key: "state", kind: "badge" }, { key: "acknowledged_at", kind: "time" }, { key: "resolved_at", kind: "time" }, { key: "detail" }];
const decisionColumns: Column[] = [{ key: "occurred_at", kind: "time" }, { key: "symbol" }, { key: "timeframe" }, { key: "stage" }, { key: "status", kind: "badge" }, { key: "reason_code" }, { key: "strategy_key" }, { key: "cycle_id" }];
const tradeColumns: Column[] = [{ key: "closed_at", kind: "time" }, { key: "symbol" }, { key: "strategy_key" }, { key: "gross_pnl", kind: "number" }, { key: "total_fees", kind: "number" }, { key: "net_pnl", kind: "number" }, { key: "return_on_allocated_capital", kind: "number" }];
const orderColumns: Column[] = [{ key: "submitted_at", kind: "time" }, { key: "symbol" }, { key: "action", kind: "badge" }, { key: "quantity", kind: "number" }, { key: "state", kind: "badge" }, { key: "fill_price", kind: "number" }, { key: "fee", kind: "number" }, { key: "strategy_key" }];

function RecordTable({ rows, view, empty, visibleColumns }: { rows: Json[]; view: View; empty?: string; visibleColumns?: Column[] }) {
  if (!rows.length) return <State kind="empty">{empty ?? "No authoritative records are available for these filters."}</State>;
  const visible: Column[] = visibleColumns ?? columns[view] ?? Object.keys(rows[0]).slice(0, 9).map((key): Column => ({ key }));
  return <div className="generic-table"><table><thead><tr>{visible.map((column) => <th key={column.key}>{column.label ?? pretty(column.key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={text(row.candidate_id ?? row.position_id ?? row.strategy_key ?? row.validation_run_id ?? row.report_id ?? row.audit_id ?? row.setting_key ?? `${view}-${index}`)}>{visible.map((column) => {
    const value = row[column.key];
    return <td key={column.key}>{column.kind === "time" ? dateTime(value) : column.kind === "badge" ? <Badge value={text(value)} /> : column.kind === "number" && number(value) !== null ? number(value)?.toLocaleString(undefined, { maximumFractionDigits: 4 }) : text(value)}</td>;
  })}</tr>)}</tbody></table></div>;
}

function MetricCard({ label, value, note }: { label: string; value: unknown; note?: string }) {
  return <article className="metric-card"><small>{label}</small><strong>{text(value)}</strong>{note && <p>{note}</p>}</article>;
}

function metricsObject(data: OperationalData, key: string): Json { const value = data.metrics?.[key]; return value && typeof value === "object" ? value as Json : {}; }

function OperationalBody({ view, data, rows, session, filters }: { view: Exclude<View, "Workspace" | "Decisions">; data: OperationalData; rows: Json[]; session: Session; filters: Filters }) {
  if (view === "Dashboard") {
    const performance = metricsObject(data, "performance"); const risk = metricsObject(data, "risk");
    return <><section className="metric-grid"><MetricCard label="Configured symbols" value={data.metrics?.universe_total ?? "—"} /><MetricCard label="NSE session" value={data.session?.state ?? "UNAVAILABLE"} note={text(data.session?.reason, "No session reason")} /><MetricCard label="Runtime" value={data.health?.worker_state ?? "UNKNOWN"} /><MetricCard label="Open positions" value={risk.open_positions ?? "—"} /><MetricCard label="Net paper P&L" value={performance.net_pnl ?? "—"} /><MetricCard label="Closed trades" value={performance.closed_trades ?? "—"} /></section>
      <div className="two-panel"><section className="panel section-panel"><PanelTitle title="Recent BUY / SELL candidates" count={data.primary?.page?.total} /><RecordTable rows={(data.primary?.items ?? []).slice(0, 8)} view="Signals" empty="No strategy candidates have been persisted." /></section><section className="panel section-panel"><PanelTitle title="Open and recent positions" count={data.secondary?.page?.total} /><RecordTable rows={(data.secondary?.items ?? []).slice(0, 8)} view="Positions" empty="No paper positions have been persisted." /></section></div>
      {(data.tertiary?.items.length ?? 0) > 0 ? <section className="panel alert-panel"><PanelTitle title="Active and historical alerts" count={data.tertiary?.page?.total} /><RecordTable rows={data.tertiary?.items ?? []} view="Logs" visibleColumns={alertColumns} /></section> : <State kind="empty">No authoritative NSE alerts are recorded.</State>}</>;
  }
  if (view === "Strategies") return <><section className="metric-grid"><MetricCard label="Registered" value={data.primary?.page?.total ?? 0} /><MetricCard label="Approved" value={(data.primary?.items ?? []).filter((row) => row.approval_state === "APPROVED").length} /><MetricCard label="Validation runs" value={data.secondary?.page?.total ?? 0} /><MetricCard label="Passed validations" value={(data.secondary?.items ?? []).filter((row) => row.passed === true).length} /></section><section className="panel section-panel"><PanelTitle title="Strategy registry" count={rows.length} /><RecordTable rows={rows} view={view} /></section><section className="panel section-panel"><PanelTitle title="Latest validation evidence" count={data.secondary?.page?.total} /><RecordTable rows={filterRows(data.secondary?.items ?? [], filters)} view="Backtests" /></section><CapabilityNotice session={session} capability="Strategy registration and approval" /></>;
  if (view === "Positions") return <><section className="metric-grid"><MetricCard label="Positions" value={data.primary?.page?.total ?? 0} /><MetricCard label="Open" value={(data.primary?.items ?? []).filter((row) => row.state === "OPEN").length} /><MetricCard label="Paper orders" value={data.secondary?.page?.total ?? 0} /><MetricCard label="Filled orders" value={(data.secondary?.items ?? []).filter((row) => row.fill_price !== null && row.fill_price !== undefined).length} /></section><section className="panel section-panel"><PanelTitle title="Paper positions" count={rows.length} /><RecordTable rows={rows} view={view} /></section><section className="panel section-panel"><PanelTitle title="Originating paper orders" count={data.secondary?.page?.total} /><RecordTable rows={filterRows(data.secondary?.items ?? [], filters)} view={view} visibleColumns={orderColumns} empty="No paper orders are recorded." /></section><CapabilityNotice session={session} capability="Position intervention" /></>;
  if (view === "Risk") { const risk = data.metrics ?? {}; return <><section className="metric-grid"><MetricCard label="Open positions" value={risk.open_positions ?? "—"} /><MetricCard label="Gross entry notional" value={risk.gross_entry_notional ?? "—"} /><MetricCard label="Realized P&L" value={risk.realized_pnl ?? "—"} /><MetricCard label="Total fees" value={risk.total_fees ?? "—"} /><MetricCard label="Unrealized P&L" value={risk.unrealized_pnl ?? "Unavailable"} /></section><UnavailableFields value={risk.unavailable_fields} /><section className="panel section-panel"><PanelTitle title="Open exposure" count={rows.length} /><RecordTable rows={rows} view={view} empty="No open paper exposure is recorded." /></section><section className="panel section-panel"><PanelTitle title="Risk decision evidence" count={data.secondary?.page?.total} /><RecordTable rows={filterRows(data.secondary?.items ?? [], filters)} view="Logs" visibleColumns={decisionColumns} empty="No risk-stage decisions are recorded." /></section><CapabilityNotice session={session} capability="Risk-limit changes" /></>; }
  if (view === "Reports") { const performance = data.metrics ?? {}; return <><section className="metric-grid"><MetricCard label="Closed trades" value={performance.closed_trades ?? "—"} /><MetricCard label="Wins" value={performance.wins ?? "—"} /><MetricCard label="Win rate" value={number(performance.win_rate) === null ? "Unavailable" : `${((number(performance.win_rate) ?? 0) * 100).toFixed(1)}%`} /><MetricCard label="Gross P&L" value={performance.gross_pnl ?? "—"} /><MetricCard label="Net P&L" value={performance.net_pnl ?? "—"} /><MetricCard label="Total fees" value={performance.total_fees ?? "—"} /></section><UnavailableFields value={performance.unavailable_fields} /><section className="panel section-panel"><PanelTitle title="Report runs" count={rows.length} /><RecordTable rows={rows} view={view} /></section><section className="panel section-panel"><PanelTitle title="Closed trade outcomes" count={data.secondary?.page?.total} /><RecordTable rows={filterRows(data.secondary?.items ?? [], filters)} view="Positions" visibleColumns={tradeColumns} empty="No closed paper outcomes are recorded." /></section><CapabilityNotice session={session} capability="Report generation and artifact download" /></>; }
  if (view === "Logs") return <><section className="panel section-panel"><PanelTitle title="Operational audit" count={rows.length} /><RecordTable rows={rows} view={view} /></section><section className="panel section-panel"><PanelTitle title="Alerts" count={data.secondary?.page?.total} /><RecordTable rows={filterRows(data.secondary?.items ?? [], filters)} view={view} visibleColumns={alertColumns} empty="No authoritative alerts are recorded." /></section><CapabilityNotice session={session} capability="Alert acknowledgement and resolution" /></>;
  if (view === "Settings") return <><section className="metric-grid"><MetricCard label="NSE session" value={data.session?.state ?? "UNAVAILABLE"} note={text(data.session?.reason)} /><MetricCard label="Holiday calendar" value={data.session?.holiday_calendar_complete ? "COMPLETE" : "INCOMPLETE"} /><MetricCard label="Runtime" value={data.health?.worker_state ?? "UNKNOWN"} /><MetricCard label="Provider" value={(data.health?.providers as Json | undefined)?.active_provider ?? "Unavailable"} /></section>{!data.session?.holiday_calendar_complete && <State kind="warning">NSE holiday-calendar completeness is not verified. Entry policy remains authoritative and fail-visible.</State>}<section className="panel section-panel"><PanelTitle title="Effective settings" count={rows.length} /><RecordTable rows={rows} view={view} /></section><CapabilityNotice session={session} capability="Settings changes and runtime controls" /></>;
  const capability: Partial<Record<View, string>> = { Universe: "Universe configuration", Positions: "Position intervention", Backtests: "Backtest execution", Reports: "Report generation" };
  return <><section className="panel section-panel"><PanelTitle title={operationalCopy[view].title} count={rows.length} /><RecordTable rows={rows} view={view} empty={view === "Signals" ? "No genuine BUY / SELL candidates have been persisted for these filters." : view === "Backtests" ? "No strategy validation runs are recorded." : undefined} /></section>{view === "Universe" && <State kind="unavailable">The current universe API exposes the enabled runtime set. A read contract for disabled historical universe rows is not available.</State>}{view === "Backtests" && <State kind="unavailable">Validation artifacts are authoritative. Dedicated backtest jobs, progress, equity curves and downloadable artifacts do not yet have a read contract.</State>}{capability[view] && <CapabilityNotice session={session} capability={capability[view]!} />}</>;
}

function PanelTitle({ title, count }: { title: string; count?: number }) { return <div className="panel-title"><h2>{title}</h2>{count !== undefined && <span>{count.toLocaleString()} records</span>}</div>; }
function UnavailableFields({ value }: { value: unknown }) { const fields = Array.isArray(value) ? value : []; return fields.length ? <State kind="unavailable">Unavailable metrics: {fields.map(pretty).join(", ")}.</State> : null; }

function OperationalView({ view, session, onFreshness }: { view: Exclude<View, "Workspace" | "Decisions">; session: Session; onFreshness: (freshest: string | null) => void }) {
  const [data, setData] = useState<OperationalData | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const [filters, setFilter, clear] = useUrlFilters(view);
  const load = useCallback(async () => { setLoading(true); setError(""); try { const next = await loadOperational(view); setData(next); onFreshness(pageFreshness(next)); } catch (reason) { setError(reason instanceof Error ? reason.message : `${view} unavailable`); } finally { setLoading(false); } }, [view, onFreshness]);
  useEffect(() => { queueMicrotask(() => void load()); }, [load]);
  if (loading) return <div className="workspace-content generic-view"><State kind="loading">Loading authoritative {view.toLowerCase()}…</State></div>;
  if (error) return <div className="workspace-content generic-view"><State kind="error"><b>{view} unavailable.</b><br />{error}<br /><button onClick={() => void load()}>Retry</button></State></div>;
  if (!data) return null;
  const allRows = [...(data.primary?.items ?? []), ...(data.secondary?.items ?? [])];
  const filterSource = view === "Strategies" ? (data.primary?.items ?? []) : allRows;
  const rows = filterRows(data.primary?.items ?? [], filters); const freshness = pageFreshness(data);
  return <div className="workspace-content generic-view"><header className="page-heading"><div><span>NSE · AUTHORITATIVE API</span><h1>{operationalCopy[view].title}</h1><p>{operationalCopy[view].description}</p></div><div><Freshness value={freshness} /><button className="refresh-button" onClick={() => void load()}>↻ Refresh</button></div></header>
    {freshnessOf(freshness) === "stale" && <State kind="stale">The newest record is older than 15 minutes. Verify the NSE session and runtime health before relying on it.</State>}
    <OperationalFilters view={view} rows={filterSource} filters={filters} setFilter={setFilter} clear={clear} />
    <OperationalBody view={view} data={data} rows={rows} session={session} filters={filters} />
  </div>;
}

function Console({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [view, setView] = useState<View>("Workspace"); const [health, setHealth] = useState<Health>(); const [freshest, setFreshest] = useState<string | null>(null);
  const snapshot = useCallback((nextHealth: Health, nextFreshest: string | null) => { setHealth(nextHealth); setFreshest(nextFreshest); }, []);
  const updateFreshness = useCallback((nextFreshest: string | null) => setFreshest(nextFreshest), []);
  useEffect(() => {
    let cancelled = false;
    const loadStatus = async () => { try { const [nextHealth, marketSession] = await Promise.all([backend<Health>("nse/health"), backend<SessionStatus>("nse/session")]); if (!cancelled) setHealth({ ...nextHealth, session: marketSession }); } catch { if (!cancelled) setHealth(undefined); } };
    void loadStatus(); const timer = window.setInterval(() => void loadStatus(), 15_000); return () => { cancelled = true; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    const read = () => { const requested = new URLSearchParams(window.location.search).get("view") as View | null; if (requested && ["Workspace", ...views.map((item) => item.name)].includes(requested)) setView(requested); };
    read(); window.addEventListener("popstate", read); return () => window.removeEventListener("popstate", read);
  }, []);
  const navigate = useCallback((next: View) => {
    setView(next); const url = new URL(window.location.href); url.searchParams.set("view", next); for (const key of filterKeys) url.searchParams.delete(key); window.history.pushState({}, "", url);
  }, []);
  const content = view === "Workspace" || view === "Decisions" ? <Workspace onSnapshot={snapshot} namespace={view} /> : <OperationalView view={view} session={session} onFreshness={updateFreshness} />;
  return <Shell session={session} view={view} setView={navigate} onLogout={onLogout} health={health} freshestAt={freshest}>{content}</Shell>;
}

export default function Home() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  useEffect(() => { fetch("/api/auth/session", { cache: "no-store" }).then(async (response) => setSession(response.ok ? await response.json() : null)).catch(() => setSession(null)); }, []);
  if (session === undefined) return <main className="login-shell"><State kind="loading">Checking secure session…</State></main>;
  if (!session) return <Login onLogin={setSession} />;
  return <Console session={session} onLogout={async () => { await fetch("/api/auth/logout", { method: "POST" }); setSession(null); }} />;
}

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

const views: { name: View; icon: string }[] = [
  { name: "Dashboard", icon: "⌂" }, { name: "Universe", icon: "◎" },
  { name: "Strategies", icon: "⌘" }, { name: "Signals", icon: "⌁" },
  { name: "Decisions", icon: "▣" }, { name: "Positions", icon: "▤" },
  { name: "Risk", icon: "◇" }, { name: "Backtests", icon: "▧" },
  { name: "Reports", icon: "□" }, { name: "Logs", icon: "≡" },
  { name: "Settings", icon: "⚙" },
];

const genericResources: Partial<Record<View, string>> = {
  Dashboard: "overview", Universe: "nse/universe?enabled=true&limit=1000", Strategies: "strategy-lab/strategies?market=nse&limit=500",
  Signals: "nse/signals?limit=500", Positions: "nse/positions?limit=500",
  Risk: "nse/risk/aggregate", Reports: "reports?market=nse&limit=500", Logs: "audit?market=nse&limit=500",
  Settings: "settings?market=nse&limit=500",
};

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

function pretty(value: unknown): string {
  return text(value).replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
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

function State({ kind, children }: { kind: "loading" | "error" | "empty" | "warning"; children: ReactNode }) {
  return <div className={`state state-${kind}`}>{children}</div>;
}

function Shell({ session, view, setView, onLogout, children, health, freshestAt }: { session: Session; view: View; setView: (view: View) => void; onLogout: () => void; children: ReactNode; health?: Health; freshestAt?: string | null }) {
  const provider = health?.providers ?? {};
  const feedState = text(provider.state, "UNAVAILABLE");
  const activeProvider = text(provider.active_provider, "No feed");
  const running = text(health?.worker_state, "UNKNOWN");
  const exchange = text(health?.session?.state, "UNKNOWN");
  const fresh = Boolean(freshestAt);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><Logo /><span><strong>NanoDelta</strong><small>QUANT RESEARCH</small></span></div>
      <button className="workspace-link selected" onClick={() => setView("Workspace")}><span>▥</span>NSE Workspace</button>
      <nav>{views.map((item) => <button key={item.name} className={(view === item.name || (view === "Workspace" && item.name === "Decisions")) ? "active" : ""} onClick={() => setView(item.name)}><span>{item.icon}</span>{item.name}</button>)}</nav>
      <div className="user-card"><span className="avatar">{session.subject.slice(0, 2).toUpperCase()}</span><div><strong>{session.subject}</strong><small>{session.role.toUpperCase()} · secure session</small></div><button aria-label="Sign out" onClick={onLogout}>⌄</button></div>
    </aside>
    <main className="workspace-main">
      <header className="topbar"><button className="menu-button" aria-label="Open navigation">☰</button>
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

function buildRows(data: WorkspaceData): { rows: WorkspaceRow[]; cycleId: string; cycleAt: string | null } {
  const all = data.decisions.items;
  const latest = [...all].sort((a, b) => text(b.occurred_at).localeCompare(text(a.occurred_at)))[0];
  const cycleId = text(latest?.cycle_id, "");
  const cycleAt = typeof latest?.occurred_at === "string" ? latest.occurred_at : null;
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

function Workspace({ onSnapshot }: { onSnapshot: (health: Health, freshest: string | null) => void }) {
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadedAt, setLoadedAt] = useState(0);
  const [selectedKey, setSelectedKey] = useState("");
  const [query, setQuery] = useState(""); const [signal, setSignal] = useState("");
  const [decision, setDecision] = useState(""); const [timeframe, setTimeframe] = useState(""); const [strategy, setStrategy] = useState(""); const [readiness, setReadiness] = useState("");
  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true); setError("");
    try {
      const [health, session, decisions, signals, universe, strategies, features, orders, positions] = await Promise.all([
        backend<Health>("nse/health"), backend<SessionStatus>("nse/session"), backend<ApiPage>("nse/decision-events?limit=500"),
        backend<ApiPage>("nse/signals?limit=500"), backend<ApiPage>("nse/universe?enabled=true&limit=1000"),
        backend<ApiPage>("strategy-lab/strategies?market=nse&limit=500"), backend<ApiPage>("nse/features?limit=500"),
        backend<ApiPage>("nse/orders?limit=500"), backend<ApiPage>("nse/positions?limit=500"),
      ]);
      const currentHealth = { ...health, session };
      const next = { health: currentHealth, decisions, signals, universe, strategies, features, orders, positions };
      setData(next); setLoadedAt(Date.now()); onSnapshot(currentHealth, features.freshness?.freshest_at ?? null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "NSE workspace unavailable"); }
    finally { setLoading(false); }
  }, [onSnapshot]);
  useEffect(() => { queueMicrotask(() => void load()); const timer = window.setInterval(() => { if (!document.hidden) void load(true); }, 10_000); return () => window.clearInterval(timer); }, [load]);
  const built = useMemo(() => data ? buildRows(data) : { rows: [], cycleId: "", cycleAt: null }, [data]);
  const filterValues = (field: "signal" | "timeframe" | "strategy" | "data") => [...new Set(built.rows.map((row) => text(row[field])).filter((value) => value !== "—"))].sort();
  const filtered = built.rows.filter((row) => (!query || row.symbol.toLowerCase().includes(query.toLowerCase())) && (!signal || row.signal === signal) && (!decision || row.decision === decision) && (!timeframe || row.timeframe === timeframe) && (!strategy || row.strategy === strategy) && (!readiness || row.data === readiness));
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
            <input aria-label="Search symbol" placeholder="Search symbol" value={query} onChange={(e) => setQuery(e.target.value)} />
            <Filter value={timeframe} onChange={setTimeframe} label="All TF" options={filterValues("timeframe")} />
            <Filter value={strategy} onChange={setStrategy} label="All strategies" options={filterValues("strategy")} />
            <Filter value={signal} onChange={setSignal} label="BUY / SELL" options={filterValues("signal")} />
            <Filter value={decision} onChange={setDecision} label="All decisions" options={["ACCEPT", "REJECT", "PENDING"]} />
            <Filter value={readiness} onChange={setReadiness} label="All data" options={["READY", "PARTIAL", "NOT READY"]} />
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

function GenericView({ view }: { view: View }) {
  const [data, setData] = useState<unknown>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const path = genericResources[view];
  useEffect(() => { queueMicrotask(() => { if (!path) { setLoading(false); return; } setLoading(true); setError(""); backend<unknown>(path).then(setData).catch((reason) => setError(reason instanceof Error ? reason.message : "Unavailable")).finally(() => setLoading(false)); }); }, [path]);
  const rows = data && typeof data === "object" && Array.isArray((data as ApiPage).items) ? (data as ApiPage).items : data && typeof data === "object" ? [data as Json] : [];
  return <div className="workspace-content generic-view"><div className="generic-title"><span>NSE · AUTHORITATIVE API</span><h1>{view}</h1><p>This page continues to use backend records; the decision workspace is the first fully composed NSE view.</p></div>
    {loading ? <State kind="loading">Loading {view.toLowerCase()}…</State> : error ? <State kind="error">{error}</State> : !path ? <State kind="empty">This NSE page has no authoritative read contract yet.</State> : <section className="panel"><div className="generic-table"><RecordTable rows={rows} /></div></section>}
  </div>;
}

function RecordTable({ rows }: { rows: Json[] }) {
  if (!rows.length) return <State kind="empty">No authoritative records are available.</State>;
  const keys = Object.keys(rows[0]).slice(0, 9);
  return <table><thead><tr>{keys.map((key) => <th key={key}>{pretty(key)}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={text(row.id ?? row.record_id ?? index)}>{keys.map((key) => <td key={key}>{text(row[key])}</td>)}</tr>)}</tbody></table>;
}

function Console({ session, onLogout }: { session: Session; onLogout: () => void }) {
  const [view, setView] = useState<View>("Workspace"); const [health, setHealth] = useState<Health>(); const [freshest, setFreshest] = useState<string | null>(null);
  const snapshot = useCallback((nextHealth: Health, nextFreshest: string | null) => { setHealth(nextHealth); setFreshest(nextFreshest); }, []);
  return <Shell session={session} view={view} setView={setView} onLogout={onLogout} health={health} freshestAt={freshest}>{view === "Workspace" || view === "Decisions" ? <Workspace onSnapshot={snapshot} /> : <GenericView view={view} />}</Shell>;
}

export default function Home() {
  const [session, setSession] = useState<Session | null | undefined>(undefined);
  useEffect(() => { fetch("/api/auth/session", { cache: "no-store" }).then(async (response) => setSession(response.ok ? await response.json() : null)).catch(() => setSession(null)); }, []);
  if (session === undefined) return <main className="login-shell"><State kind="loading">Checking secure session…</State></main>;
  if (!session) return <Login onLogin={setSession} />;
  return <Console session={session} onLogout={async () => { await fetch("/api/auth/logout", { method: "POST" }); setSession(null); }} />;
}

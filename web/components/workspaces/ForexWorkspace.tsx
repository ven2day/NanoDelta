"use client";

import {
  Activity,
  BrainCircuit,
  Clock,
  Database,
  Filter,
  Gauge,
  Radio,
  ShieldCheck,
  TrendingUp,
  Wallet,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { SixLayerLifecycle, type LifecycleLayer } from "@/components/SixLayerLifecycle";
import { useMarketWorkspace } from "@/lib/useMarketWorkspace";
import type { MarketDecision, MarketStatus, MarketTechnicalCandidate } from "@/lib/marketApi";

/* ------------------------------------------------------------------ helpers */

type Rec = Record<string, unknown>;

const asRec = (value: unknown): Rec =>
  value && typeof value === "object" ? (value as Rec) : {};
const numOf = (rec: Rec, key: string): number => {
  const v = rec[key];
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
};
const dir = (item: MarketDecision): string =>
  String(item.side ?? item.signal_type ?? "-").toUpperCase();

/** ISO timestamp → HH:MM:SS in a fixed timezone (UTC) or local. */
function clockOf(date: Date, utc: boolean): string {
  const h = utc ? date.getUTCHours() : date.getHours();
  const m = utc ? date.getUTCMinutes() : date.getMinutes();
  const s = utc ? date.getUTCSeconds() : date.getSeconds();
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

/** Human "age" between an ISO timestamp and now (e.g. "5s", "2m 14s", "3h 18m"). */
function ageSince(iso: string | null | undefined, now: Date): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "—";
  const secs = Math.max(0, Math.round((now.getTime() - then) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

/** ISO timestamp → "HH:MM UTC" (or full time). */
function isoClock(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${clockOf(d, true).slice(0, 5)} UTC`;
}

const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);

/** Compact, human status for a live signal (why it isn't a trade yet). */
function signalStatus(item: {
  multi_timeframe?: { conflicted?: boolean };
  eligibility_status?: string;
  rejection_reason?: string | null;
}): string {
  if (item.multi_timeframe?.conflicted) return "MTF conflict";
  const elig = String(item.eligibility_status ?? "").toUpperCase();
  if (elig === "PAPER_APPROVED" || elig === "LIVE_APPROVED") return "Approved";
  if (elig === "SHADOW" || elig === "UNVALIDATED") return "Shadow — not approved";
  const reason = String(item.rejection_reason ?? "").replace(/_/g, " ").toLowerCase();
  return reason ? reason.charAt(0).toUpperCase() + reason.slice(1) : "Pending";
}

const pct = (value: number, digits = 2) => `${value.toFixed(digits)}%`;

/** Format an FX price at instrument precision (JPY pairs quote to 3 dp, else 5). */
const fxPrice = (value: number, symbol?: string): string => {
  if (!value) return "—";
  const jpy = String(symbol ?? "").toUpperCase().includes("JPY");
  return value.toFixed(jpy ? 3 : 5);
};

/** Sum a metric field across the intraday + scalp cycle metric blocks. */
function funnelSum(status: MarketStatus | null, key: string): number {
  const intraday = asRec(status?.cycle_metrics);
  const scalp = asRec(status?.scalp_cycle_metrics);
  return numOf(intraday, key) + numOf(scalp, key);
}

/** Deterministic FX session clock from UTC — market state, never fabricated. */
function forexSessions(now: Date) {
  const h = now.getUTCHours() + now.getUTCMinutes() / 60;
  const day = now.getUTCDay(); // 0 Sun .. 6 Sat
  // FX week: closed Fri 22:00 UTC → Sun 22:00 UTC.
  const weekendClosed =
    day === 6 || (day === 0 && h < 22) || (day === 5 && h >= 22);
  const inRange = (start: number, end: number) =>
    start < end ? h >= start && h < end : h >= start || h < end;
  const sessions = [
    { name: "Sydney", open: !weekendClosed && inRange(22, 7) },
    { name: "Tokyo", open: !weekendClosed && inRange(0, 9) },
    { name: "London", open: !weekendClosed && inRange(7, 16) },
    { name: "New York", open: !weekendClosed && inRange(12, 21) },
  ];
  const openNames = sessions.filter((s) => s.open).map((s) => s.name);
  const overlap =
    openNames.includes("London") && openNames.includes("New York")
      ? "LONDON + NEW YORK OVERLAP"
      : openNames.length > 0
        ? openNames.map((n) => n.toUpperCase()).join(" + ")
        : "MARKET CLOSED";
  return { sessions, weekendClosed, marketOpen: openNames.length > 0, overlap };
}

const STRATEGY_ORDER = [
  "ema_adx_trend",
  "donchian_breakout",
  "time_series_momentum",
  "trend_pullback",
  "supertrend_adx_ema",
  "macd_trend_continuation",
  "bollinger_rsi_mean_reversion",
  "vwap_mean_reversion",
  "opening_range_breakout",
  "relative_strength_momentum",
];
const DISABLED_STRATEGIES = new Set([
  "vwap_mean_reversion",
  "opening_range_breakout",
  "relative_strength_momentum",
]);
const MATRIX_TIMEFRAMES = ["5m", "15m", "30m", "1h", "4h"];
const STRATEGY_LABEL: Record<string, string> = {
  macd_trend_continuation: "macd_trend_cont.",
  bollinger_rsi_mean_reversion: "bollinger_rsi",
  relative_strength_momentum: "relative_strength",
};

/* --------------------------------------------------------------- primitives */

function StatTile({
  label,
  value,
  sub,
  tone,
  icon: Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  tone: "good" | "warning" | "critical" | "neutral";
  icon: LucideIcon;
}) {
  const color =
    tone === "good"
      ? "var(--status-good)"
      : tone === "warning"
        ? "var(--status-warning)"
        : tone === "critical"
          ? "var(--status-critical)"
          : "var(--ink-secondary)";
  return (
    <div className="rounded-xl border border-border bg-surface px-4 py-3 shadow-card">
      <div className="flex items-center justify-between">
        <span className="text-[0.7rem] font-semibold uppercase tracking-[0.16em] text-ink-muted">
          {label}
        </span>
        <Icon size={14} style={{ color }} />
      </div>
      <div className="mt-2 flex items-center gap-1.5">
        <span
          className="h-1.5 w-1.5 rounded-full"
          style={{ backgroundColor: color }}
        />
        <span className="text-base font-semibold text-ink-primary">{value}</span>
      </div>
      {sub && <p className="mt-0.5 text-xs text-ink-muted">{sub}</p>}
    </div>
  );
}

function KeyValue({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  tone?: "good" | "warning" | "critical";
}) {
  const color =
    tone === "good"
      ? "text-status-good"
      : tone === "warning"
        ? "text-status-warning"
        : tone === "critical"
          ? "text-status-critical"
          : "text-ink-primary";
  return (
    <div className="flex items-center justify-between py-1.5 text-xs">
      <span className="text-ink-muted">{label}</span>
      <span className={`font-medium ${color}`}>{value}</span>
    </div>
  );
}

function HealthRow({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <div className="flex items-center justify-between border-t border-border py-2 text-xs first:border-t-0">
      <span className="text-ink-secondary">{label}</span>
      <span className="flex items-center gap-2">
        {detail && <span className="text-ink-muted">{detail}</span>}
        <span
          className="h-2 w-2 rounded-full"
          style={{
            backgroundColor: ok ? "var(--status-good)" : "var(--status-critical)",
          }}
        />
      </span>
    </div>
  );
}

function TimeItem({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-ink-muted">{label}</span>
      <span className="text-right">
        <span className="font-medium tabular-nums text-ink-primary">{value}</span>
        {hint && <span className="ml-1 text-[0.7rem] text-ink-muted">{hint}</span>}
      </span>
    </div>
  );
}

/* ---------------------------------------------------------------- workspace */

export function ForexWorkspace() {
  const { status, candidates, signals, positions, strategies, models, error } =
    useMarketWorkspace("FOREX");

  // Live 1-second clock for UTC/local time + freshness ages.
  const [now, setNow] = useState<Date>(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);
  // Candidate selected for the lifecycle drawer.
  const [selected, setSelected] = useState<MarketTechnicalCandidate | null>(null);

  const environment = String(status?.environment ?? "PRACTICE").toUpperCase();
  const snapshotStale = String(status?.health_state ?? "").toUpperCase() === "STALE";
  const healthy = status?.status === "HEALTHY" && !snapshotStale;
  const stream = snapshotStale
    ? "STALE"
    : String(status?.pricing_stream ?? "NOT_STARTED").toUpperCase();
  const ageSeconds = typeof status?.age_seconds === "number" ? status.age_seconds : null;
  const cycleNumber = typeof status?.cycle === "number" ? status.cycle : null;

  const clock = forexSessions(new Date());
  const marketOpen = !snapshotStale && clock.marketOpen;

  const statusRec = asRec(status);
  const equity = numOf(statusRec, "account_equity") || 100000;
  const homeCcy = String(status?.account_home_currency ?? "USD");
  const dailyLossFraction = numOf(statusRec, "daily_loss_limit_fraction") || 0.02;
  const maxMarginUtil = numOf(statusRec, "max_margin_utilization") || 0.5;

  // Risk aggregates derived from the real open-position payloads.
  const usedMargin = positions.reduce(
    (sum, p) => sum + numOf(asRec(p), "margin_required_home"),
    0,
  );
  const openRisk = positions.reduce(
    (sum, p) => sum + numOf(asRec(p), "risk_amount_home"),
    0,
  );
  const exposures: Record<string, number> = {};
  positions.forEach((p) => {
    const exp = asRec(asRec(p).currency_exposure_home);
    Object.entries(exp).forEach(([ccy, amount]) => {
      exposures[ccy] = (exposures[ccy] ?? 0) + (typeof amount === "number" ? amount : 0);
    });
  });
  const availableMargin = equity * maxMarginUtil - usedMargin;

  // Funnel across both horizons.
  const stratEvals = funnelSum(status, "strategy_evaluations");
  const techBuy = funnelSum(status, "technical_buy_candidates");
  const techSell = funnelSum(status, "technical_sell_candidates");
  const eligible = funnelSum(status, "eligibility_passed");
  const reviewed =
    funnelSum(status, "ml_evaluated") + funnelSum(status, "qwen_reviewed");
  const finalBuy = funnelSum(status, "final_buy");
  const finalSell = funnelSum(status, "final_sell");
  // Full lifecycle funnel — every stage the backend actually tracks, current cycle.
  const consolidated = funnelSum(status, "consolidated_candidates");
  const mtfBlocked = funnelSum(status, "multi_timeframe_blocked");
  const funnel: { label: string; value: number; buy?: number; sell?: number }[] = [
    { label: "Strategy Evaluations", value: stratEvals },
    { label: "Raw Signals", value: techBuy + techSell, buy: techBuy, sell: techSell },
    { label: "Consolidated", value: consolidated },
    { label: "MTF Confirmed", value: Math.max(0, consolidated - mtfBlocked) },
    { label: "Entry Quality", value: funnelSum(status, "entry_quality_passed") },
    { label: "Strategy Eligible", value: eligible },
    { label: "ML Evaluated", value: funnelSum(status, "ml_evaluated") },
    { label: "Qwen Reviewed", value: funnelSum(status, "qwen_reviewed") },
    { label: "Risk Approved", value: funnelSum(status, "risk_approved") },
    {
      label: "Final BUY / SELL",
      value: finalBuy + finalSell,
      buy: finalBuy,
      sell: finalSell,
    },
    { label: "Paper Orders", value: funnelSum(status, "paper_orders") },
  ];
  const closedTrades = numOf(statusRec, "positions_closed") || numOf(statusRec, "closed_trades");
  const lifecycleLayers: LifecycleLayer[] = [
    { id: "raw", value: stream === "HEALTHY" ? "Streaming" : stream, detail: "OANDA pricing and candle payloads", state: healthy ? "active" : "idle" },
    { id: "canonical", value: candleAt ? isoClock(candleAt) : "No candle", detail: "Normalized FX candles and instruments", state: candleAt ? "ready" : "idle" },
    { id: "feature", value: `${stratEvals} evaluations`, detail: "Current-cycle features and regimes", state: stratEvals > 0 ? "ready" : "idle" },
    { id: "decision", value: `${finalBuy + finalSell} BUY / SELL`, detail: `${candidates.length} candidates through evidence gates`, state: finalBuy + finalSell > 0 ? "ready" : "idle" },
    { id: "execution", value: `${positions.length} open`, detail: `${funnelSum(status, "paper_orders")} current-cycle paper orders`, state: positions.length > 0 ? "active" : "idle" },
    { id: "outcome", value: closedTrades > 0 ? `${closedTrades} closed` : "Awaiting close", detail: "Performance and attribution records", state: closedTrades > 0 ? "ready" : "idle" },
  ];
  const llmToday = asRec(status?.llm_finops);

  // Rejection / bottleneck analytics (current cycle) — from backend reason codes.
  const rejectionSummary = Array.isArray(status?.rejection_summary)
    ? (status?.rejection_summary as Rec[])
    : [];
  const rejectedTotal = numOf(statusRec, "rejected_total");
  const biggestBottleneck = rejectionSummary[0];

  // Real timestamps + cycle timing from the enriched snapshot.
  const scanAt = (status?.scan_completed_at as string | undefined) ?? null;
  const candleAt = (status?.latest_candle_timestamp as string | undefined) ?? null;
  const pricingAt = (status?.pricing_snapshot_at as string | undefined) ?? null;
  const cycleMs = numOf(statusRec, "cycle_duration_ms");
  const prevCycleMs = numOf(statusRec, "previous_cycle_duration_ms");
  const p50Ms = numOf(statusRec, "cycle_p50_ms");
  const p95Ms = numOf(statusRec, "cycle_p95_ms");

  // Strategy matrix pivot.
  const matrix: Record<string, Record<string, string>> = {};
  strategies.forEach((s) => {
    const name = String(s.strategy_name ?? s.strategy ?? "");
    const tf = String(s.timeframe ?? "");
    const st = String(s.validation_status ?? s.eligibility ?? "").toUpperCase();
    if (!name || !tf) return;
    (matrix[name] ??= {})[tf] = st;
  });
  const presentStrategies = STRATEGY_ORDER.filter(
    (name) => matrix[name] || DISABLED_STRATEGIES.has(name),
  );

  const timescaleOk = status?.timescale_connected !== false && !snapshotStale;
  const registryOk = status?.model_registry_connected === true;
  const killForex = status?.forex_kill_switch === true;
  const killGlobal = status?.global_kill_switch === true;

  return (
    <section className="space-y-4" aria-label="FOREX workspace">
      {/* Header + clock */}
      <div className="rounded-xl border border-border bg-surface px-5 py-4 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight text-ink-primary">
              FOREX <span className="text-ink-muted">/ OANDA PRACTICE</span>
            </h1>
            <Badge
              label={healthy ? "RUNTIME RUNNING" : snapshotStale ? "STALE" : "STARTING"}
              tone={healthy ? "good" : "warning"}
              dot
              pulse={healthy}
            />
            {cycleNumber !== null && (
              <span className="text-xs font-medium text-ink-muted">Cycle #{cycleNumber}</span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge label={environment} tone={environment === "LIVE" ? "critical" : "neutral"} />
            <Badge label={`EXECUTION: ${status?.execution ?? "PAPER"}`} tone="good" />
            <Badge label="BROKER ORDERS: OFF" tone="good" />
          </div>
        </div>
        {/* Real timestamps — never a bare "60s" without an anchor. */}
        <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 border-t border-border pt-3 text-xs sm:grid-cols-3 lg:grid-cols-4">
          <TimeItem label="UTC" value={clockOf(now, true)} />
          <TimeItem label="Local" value={clockOf(now, false)} />
          <TimeItem label="Session" value={clock.overlap} />
          <TimeItem label="Last scan" value={`${ageSince(scanAt, now)} ago`} hint={isoClock(scanAt)} />
          <TimeItem label="Last candle" value={isoClock(candleAt)} hint={`${ageSince(candleAt, now)} old`} />
          <TimeItem label="Pricing" value={`${ageSince(pricingAt, now)} ago`} hint={isoClock(pricingAt)} />
          <TimeItem label="Cycle runtime" value={cycleMs ? ms(cycleMs) : "—"} hint={prevCycleMs ? `prev ${ms(prevCycleMs)}` : undefined} />
          <TimeItem label="P50 / P95" value={p50Ms ? `${ms(p50Ms)} / ${ms(p95Ms)}` : "—"} />
        </div>
      </div>

      {error && (
        <p className="rounded-lg border border-border bg-surface px-4 py-2 text-xs text-status-warning">
          Forex API: {error}
          {error.includes("401") && " — your dashboard session expired; sign out and back in."}
        </p>
      )}
      {snapshotStale && (
        <p className="rounded-lg border border-border bg-surface px-4 py-2 text-xs font-medium text-status-warning">
          Dashboard snapshot is stale/disconnected
          {ageSeconds === null ? "." : ` (${Math.round(ageSeconds)}s old) — start the Forex worker.`}
        </p>
      )}

      {/* Status strip */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="OANDA"
          value={healthy ? "CONNECTED" : "UNAVAILABLE"}
          sub={environment}
          tone={healthy ? "good" : "critical"}
          icon={Radio}
        />
        <StatTile
          label="Pricing Stream"
          value={stream}
          sub={pricingAt ? `updated ${ageSince(pricingAt, now)} ago` : "heartbeat monitored"}
          tone={stream === "HEALTHY" ? "good" : "warning"}
          icon={Activity}
        />
        <StatTile
          label="Forex Market"
          value={marketOpen ? "OPEN" : "CLOSED"}
          sub={clock.overlap}
          tone={marketOpen ? "good" : "neutral"}
          icon={Clock}
        />
        <StatTile
          label="Runtime"
          value={healthy ? "RUNNING" : "IDLE"}
          sub={cycleMs ? `cycle ${ms(cycleMs)}` : "settled-candle cycles"}
          tone={healthy ? "good" : "warning"}
          icon={Gauge}
        />
      </div>

      <SixLayerLifecycle market="FOREX" provider="OANDA PRACTICE" layers={lifecycleLayers} />

      <Card title="Forex LLM Today" icon={BrainCircuit} accent="var(--cat-4)">
        <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <span>Calls {numOf(llmToday, "calls")}</span>
          <span>Candidate {numOf(llmToday, "candidate_review_calls")}</span>
          <span>Tokens {numOf(llmToday, "total_tokens").toLocaleString()}</span>
          <span>Cost ${numOf(llmToday, "total_cost_usd").toFixed(4)}</span>
        </div>
      </Card>

      {/* Final trade decisions */}
      <Card title="Final Trade Decisions" icon={TrendingUp} accent="var(--cat-1)">
        {signals.length === 0 ? (
          <p className="py-8 text-center text-xs text-ink-muted">
            No qualified final decisions — candidates below are gated until a strategy
            passes out-of-sample validation (PAPER_APPROVED).
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-xs">
              <thead>
                <tr className="border-b border-border text-left text-ink-muted">
                  <th className="py-2 font-medium">Pair</th>
                  <th className="py-2 font-medium">TF</th>
                  <th className="py-2 font-medium">Decision</th>
                  <th className="py-2 font-medium">Strategy</th>
                  <th className="py-2 font-medium">Conf.</th>
                  <th className="py-2 font-medium">Entry</th>
                  <th className="py-2 font-medium">Stop</th>
                  <th className="py-2 font-medium">Target</th>
                </tr>
              </thead>
              <tbody>
                {signals.slice(0, 25).map((item, i) => {
                  const rec = asRec(item);
                  const side = dir(item);
                  return (
                    <tr
                      key={String(item.decision_id ?? `${item.symbol}-${item.timeframe}-${i}`)}
                      className="border-b border-border/60"
                    >
                      <td className="py-2 font-medium text-ink-primary">
                        {String(item.symbol ?? item.instrument ?? "-")}
                      </td>
                      <td className="py-2 text-ink-secondary">{String(item.timeframe ?? "-")}</td>
                      <td
                        className={`py-2 font-semibold ${side === "BUY" ? "text-status-good" : "text-status-critical"}`}
                      >
                        {side}
                      </td>
                      <td className="py-2 text-ink-secondary">{String(item.strategy ?? "-")}</td>
                      <td className="py-2 text-ink-secondary">
                        {typeof item.confidence === "number"
                          ? pct(item.confidence * 100, 0)
                          : "—"}
                      </td>
                      <td className="py-2 text-ink-secondary">
                        {fxPrice(numOf(rec, "entry_price"), String(item.symbol))}
                      </td>
                      <td className="py-2 text-ink-secondary">
                        {fxPrice(numOf(rec, "stop_loss"), String(item.symbol))}
                      </td>
                      <td className="py-2 text-ink-secondary">
                        {fxPrice(numOf(rec, "target_price"), String(item.symbol))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Live signals */}
      <Card
        title="Live Signals"
        icon={Activity}
        accent="var(--cat-3)"
        right={<span className="text-xs text-ink-muted">{candidates.length} this cycle</span>}
      >
        {candidates.length === 0 ? (
          <p className="py-8 text-center text-xs text-ink-muted">
            No signals in the latest settled-candle event.
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-xs">
                <thead>
                  <tr className="border-b border-border text-left text-ink-muted">
                    <th className="py-2 font-medium">Pair</th>
                    <th className="py-2 font-medium">TF</th>
                    <th className="py-2 font-medium">Side</th>
                    <th className="py-2 font-medium">Strategy</th>
                    <th className="py-2 font-medium">Score</th>
                    <th className="py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.slice(0, 40).map((item, i) => {
                    const side = dir(item);
                    const score =
                      typeof item.technical_score === "number"
                        ? pct(item.technical_score * 100, 0)
                        : "—";
                    return (
                      <tr
                        key={String(
                          item.candidate_id ?? `${item.symbol}-${item.timeframe}-${i}`,
                        )}
                        className="cursor-pointer border-b border-border/60 hover:bg-surface-raised"
                        onClick={() => setSelected(item)}
                        title="Open full decision trace"
                      >
                        <td className="py-2 font-medium text-ink-primary">
                          {String(item.symbol ?? item.instrument)}
                        </td>
                        <td className="py-2 text-ink-secondary">{String(item.timeframe ?? "-")}</td>
                        <td
                          className={`py-2 font-semibold ${side === "BUY" ? "text-status-good" : "text-status-critical"}`}
                        >
                          {side}
                        </td>
                        <td className="py-2 text-ink-secondary">{String(item.strategy ?? "-")}</td>
                        <td className="py-2 text-ink-secondary">{score}</td>
                        <td className="py-2 text-ink-muted">{signalStatus(item)} ›</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="mt-3 border-t border-border pt-2 text-[0.7rem] text-ink-muted">
              Signals are generated every cycle. They become trades only once their strategy is
              paper-approved — see the funnel and strategy status below.
            </p>
          </>
        )}
      </Card>

      {/* Positions + sessions */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Open Positions" icon={Wallet} accent="var(--cat-5)">
          {positions.length === 0 ? (
            <p className="py-8 text-center text-xs text-ink-muted">No open Forex positions.</p>
          ) : (
            <div className="space-y-3">
              {positions.map((item, i) => {
                const rec = asRec(item);
                const side = dir(item);
                const pnl = numOf(rec, "unrealized_pnl_home") || numOf(rec, "unrealized_pnl");
                const pips = numOf(rec, "unrealized_pips");
                return (
                  <div
                    key={String(item.position_id ?? `${item.symbol}-${i}`)}
                    className="rounded-lg border border-border p-3"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-ink-primary">
                        {String(item.symbol ?? item.instrument)}
                      </span>
                      <span
                        className={`text-xs font-semibold ${side === "BUY" ? "text-status-good" : "text-status-critical"}`}
                      >
                        {side}
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-x-4 text-xs">
                      <KeyValue label="Entry" value={fxPrice(numOf(rec, "entry_price"), String(item.symbol))} />
                      <KeyValue label="Current" value={fxPrice(numOf(rec, "current_price"), String(item.symbol))} />
                      <KeyValue label="Stop" value={fxPrice(numOf(rec, "stop_price"), String(item.symbol))} />
                      <KeyValue label="Target" value={fxPrice(numOf(rec, "target_price"), String(item.symbol))} />
                      <KeyValue label="Units" value={numOf(rec, "units").toLocaleString() || "—"} />
                      <KeyValue
                        label="Risk"
                        value={equity ? pct((numOf(rec, "risk_amount_home") / equity) * 100) : "—"}
                      />
                    </div>
                    {(numOf(rec, "current_price") > 0) && (
                      <div className="mt-2 flex items-center justify-between rounded-lg border border-border bg-surface-raised px-3 py-2">
                        <span className="text-[0.65rem] uppercase tracking-wide text-ink-muted">
                          Unrealized P/L
                        </span>
                        <span
                          className={`text-sm font-semibold ${pnl >= 0 ? "text-status-good" : "text-status-critical"}`}
                        >
                          {pips >= 0 ? "+" : ""}
                          {pips.toFixed(1)} pips
                          <span className="ml-2 text-xs">
                            {pnl >= 0 ? "+" : ""}
                            {pnl.toFixed(2)} {homeCcy}
                          </span>
                        </span>
                      </div>
                    )}
                    {(() => {
                      const entry = numOf(rec, "entry_price");
                      const stop = numOf(rec, "stop_price");
                      const target = numOf(rec, "target_price");
                      const cur = numOf(rec, "current_price");
                      if (!entry || !stop || !target || !cur) return null;
                      const span = Math.abs(target - stop);
                      const prog = span ? Math.min(100, Math.max(0, (Math.abs(cur - stop) / span) * 100)) : 0;
                      return (
                        <div className="mt-2">
                          <div className="flex justify-between text-[0.65rem] text-ink-muted">
                            <span>Stop</span>
                            <span>Target</span>
                          </div>
                          <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-status-critical/30">
                            <div
                              className="h-full rounded-full bg-status-good"
                              style={{ width: `${prog}%` }}
                            />
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        <Card title="Market / Session" icon={Clock} accent="var(--cat-2)">
          <div className="space-y-1">
            {clock.sessions.map((s) => (
              <div
                key={s.name}
                className="flex items-center justify-between border-t border-border py-2 text-xs first:border-t-0"
              >
                <span className="text-ink-secondary">{s.name}</span>
                <span className="flex items-center gap-1.5">
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{
                      backgroundColor: s.open
                        ? "var(--status-good)"
                        : "var(--ink-muted)",
                    }}
                  />
                  <span className={s.open ? "text-status-good" : "text-ink-muted"}>
                    {s.open ? "OPEN" : "CLOSED"}
                  </span>
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 rounded-lg border border-border bg-surface-raised px-3 py-2 text-xs">
            <span className="text-ink-muted">Current: </span>
            <span className="font-medium text-ink-primary">{clock.overlap}</span>
          </div>
        </Card>
      </div>

      {/* Current-cycle lifecycle funnel + rejection analytics */}
      <div className="grid gap-4 xl:grid-cols-3">
        <Card
          title="Current Cycle Funnel"
          icon={Filter}
          accent="var(--cat-3)"
          className="xl:col-span-2"
          right={
            cycleNumber !== null ? (
              <span className="text-xs text-ink-muted">Cycle #{cycleNumber}</span>
            ) : undefined
          }
        >
          <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 lg:grid-cols-6">
            {funnel.map((stage) => (
              <div key={stage.label} className="rounded-lg border border-border p-2.5">
                <p className="text-lg font-semibold tabular-nums text-ink-primary">
                  {stage.value.toLocaleString()}
                </p>
                <p className="mt-0.5 text-[0.65rem] uppercase leading-tight tracking-wide text-ink-muted">
                  {stage.label}
                </p>
                {(stage.buy !== undefined || stage.sell !== undefined) && (
                  <p className="mt-1 text-[0.7rem]">
                    <span className="text-status-good">B {stage.buy}</span>
                    <span className="mx-1 text-ink-muted">·</span>
                    <span className="text-status-critical">S {stage.sell}</span>
                  </p>
                )}
              </div>
            ))}
          </div>
          <p className="mt-3 border-t border-border pt-2 text-[0.7rem] text-ink-muted">
            Counts are for the current settled-candle cycle only — open positions from earlier
            cycles are shown separately and never mixed into these numbers.
          </p>
        </Card>

        <Card title="Why Did Candidates Die?" icon={Filter} accent="var(--status-critical)">
          {rejectionSummary.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-muted">
              No rejected candidates this cycle.
            </p>
          ) : (
            <>
              <div className="mb-3 rounded-lg border border-border bg-surface-raised px-3 py-2">
                <p className="text-[0.65rem] uppercase tracking-wide text-ink-muted">
                  Biggest bottleneck
                </p>
                <p className="mt-0.5 text-sm font-semibold text-status-critical">
                  {String(biggestBottleneck?.reason ?? "—")}
                </p>
                <p className="text-xs text-ink-muted">
                  {numOf(asRec(biggestBottleneck), "count")} of {rejectedTotal} ·{" "}
                  {numOf(asRec(biggestBottleneck), "pct")}%
                </p>
              </div>
              <div className="space-y-1.5">
                {rejectionSummary.map((r) => {
                  const reason = String(r.reason);
                  const count = numOf(r, "count");
                  const pctVal = numOf(r, "pct");
                  return (
                    <div key={reason}>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-ink-secondary">{reason}</span>
                        <span className="text-ink-muted">
                          {count} · {pctVal}%
                        </span>
                      </div>
                      <div className="mt-0.5 h-1.5 w-full overflow-hidden rounded-full bg-border">
                        <div
                          className="h-full rounded-full bg-status-critical"
                          style={{ width: `${Math.min(100, pctVal)}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </Card>
      </div>

      {/* Strategy matrix + ML status */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Strategy Status" icon={ShieldCheck} accent="var(--status-good)">
          {presentStrategies.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-muted">
              No published validator state.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[420px] text-xs">
                <thead>
                  <tr className="text-ink-muted">
                    <th className="py-1 text-left font-medium">Strategy</th>
                    {MATRIX_TIMEFRAMES.map((tf) => (
                      <th key={tf} className="px-1 py-1 text-center font-medium">
                        {tf}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {presentStrategies.map((name) => {
                    const off = DISABLED_STRATEGIES.has(name);
                    return (
                      <tr key={name} className="border-t border-border">
                        <td className="py-1.5 pr-2 text-ink-secondary">
                          {STRATEGY_LABEL[name] ?? name}
                        </td>
                        {off ? (
                          <td
                            colSpan={MATRIX_TIMEFRAMES.length}
                            className="py-1.5 text-center text-[0.7rem] font-medium text-ink-muted"
                          >
                            OFF
                          </td>
                        ) : (
                          MATRIX_TIMEFRAMES.map((tf) => {
                            const st = matrix[name]?.[tf];
                            const approved = st === "PAPER_APPROVED" || st === "LIVE_APPROVED";
                            const shadow = st === "SHADOW";
                            return (
                              <td key={tf} className="px-1 py-1.5 text-center">
                                {!st ? (
                                  <span className="text-ink-muted/40">—</span>
                                ) : approved ? (
                                  <span className="text-status-good" title={st}>
                                    ✓
                                  </span>
                                ) : (
                                  <span
                                    className={shadow ? "text-status-warning" : "text-ink-muted"}
                                    title={st}
                                  >
                                    ●
                                  </span>
                                )}
                              </td>
                            );
                          })
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="mt-3 flex flex-wrap gap-3 border-t border-border pt-2 text-[0.7rem] text-ink-muted">
                <span>
                  <span className="text-status-good">✓</span> paper-approved
                </span>
                <span>
                  <span className="text-status-warning">●</span> shadow (not executable)
                </span>
                <span>— not registered</span>
              </p>
            </div>
          )}
        </Card>

        <Card title="Model / Learning Status" icon={BrainCircuit} accent="var(--cat-4)">
          <p className="mb-3 text-xs text-ink-muted">
            Live uses ACTIVE approved models, never the latest trained.
          </p>
          {models.length === 0 ? (
            <p className="py-6 text-center text-xs text-ink-muted">
              No ACTIVE Forex model yet — models train once grains are paper-approved.
            </p>
          ) : (
            <div className="space-y-1">
              {models.slice(0, 20).map((item, i) => (
                <div
                  key={`${item.strategy_name}-${item.timeframe}-${i}`}
                  className="grid grid-cols-[1fr_auto_auto] items-center gap-2 border-t border-border py-2 text-xs first:border-t-0"
                >
                  <span className="text-ink-secondary">{item.strategy_name}</span>
                  <span className="text-ink-muted">{item.timeframe}</span>
                  <span className="font-medium text-ink-primary">
                    {String(item.status ?? item.model_version ?? "—")}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>

      {/* Risk + system health */}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Forex Risk" icon={Gauge} accent="var(--cat-1)">
          <KeyValue
            label="Account Equity"
            value={`$${equity.toLocaleString()} ${homeCcy}`}
          />
          <KeyValue label="Available Margin" value={`$${Math.round(availableMargin).toLocaleString()}`} />
          <KeyValue
            label="Used Margin"
            value={pct((usedMargin / equity) * 100)}
          />
          <KeyValue label="Open Risk" value={pct((openRisk / equity) * 100)} />
          <KeyValue
            label="Daily Loss Limit"
            value={pct(dailyLossFraction * 100)}
          />
          <div className="mt-2 border-t border-border pt-2">
            {Object.keys(exposures).length === 0 ? (
              <p className="py-1 text-xs text-ink-muted">No open currency exposure.</p>
            ) : (
              Object.entries(exposures)
                .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
                .slice(0, 6)
                .map(([ccy, amount]) => (
                  <KeyValue
                    key={ccy}
                    label={`${ccy} Exposure`}
                    value={`${amount >= 0 ? "+" : ""}${pct((amount / equity) * 100)}`}
                    tone={amount >= 0 ? "good" : "critical"}
                  />
                ))
            )}
          </div>
          <div className="mt-3">
            <Badge
              label={killForex || killGlobal ? "RISK HALTED — KILL SWITCH ON" : "RISK HEALTHY"}
              tone={killForex || killGlobal ? "critical" : "good"}
              dot
            />
          </div>
        </Card>

        <Card title="System Health" icon={Database} accent="var(--cat-2)">
          <HealthRow label="OANDA REST" ok={healthy} detail={environment} />
          <HealthRow label="OANDA Pricing Stream" ok={stream === "HEALTHY"} detail={stream} />
          <HealthRow label="TimescaleDB (candles)" ok={timescaleOk} />
          <HealthRow label="Model Registry" ok={registryOk} />
          <HealthRow
            label="Dashboard Snapshot"
            ok={!snapshotStale}
            detail={ageSeconds === null ? undefined : `${Math.round(ageSeconds)}s`}
          />
          <HealthRow label="Forex Kill Switch" ok={!killForex} detail={killForex ? "ON" : "OFF"} />
          <HealthRow label="Global Kill Switch" ok={!killGlobal} detail={killGlobal ? "ON" : "OFF"} />
        </Card>
      </div>

      {selected && <CandidateDrawer candidate={selected} onClose={() => setSelected(null)} />}
    </section>
  );
}

/* -------------------------------------------------------- candidate drawer */

function DrawerSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-t border-border py-3 first:border-t-0">
      <p className="mb-1.5 text-[0.65rem] font-semibold uppercase tracking-wide text-ink-muted">
        {title}
      </p>
      {children}
    </div>
  );
}

function CandidateDrawer({
  candidate,
  onClose,
}: {
  candidate: MarketTechnicalCandidate;
  onClose: () => void;
}) {
  const rec = asRec(candidate);
  const side = dir(candidate);
  const symbol = String(candidate.symbol ?? candidate.instrument ?? "-");
  const mtf = candidate.multi_timeframe;
  const entry = numOf(rec, "entry_price");
  const stop = numOf(rec, "stop_loss") || numOf(rec, "stop_price");
  const target = numOf(rec, "target_price");
  const rr = entry && stop && target && entry !== stop
    ? Math.abs(target - entry) / Math.abs(entry - stop)
    : null;
  const state = signalStatus(candidate);
  return (
    <div className="fixed inset-0 z-50 flex justify-end" role="dialog" aria-label="Candidate trace">
      <div className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div className="relative h-full w-full max-w-md overflow-y-auto border-l border-border bg-surface p-5 shadow-card">
        <div className="mb-3 flex items-start justify-between">
          <div>
            <p className="text-base font-semibold text-ink-primary">
              {symbol}{" "}
              <span className={side === "BUY" ? "text-status-good" : "text-status-critical"}>
                {side}
              </span>
            </p>
            <p className="text-xs text-ink-muted">
              {String(candidate.timeframe ?? "-")} · {String(candidate.strategy ?? "-")}
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-md border border-border px-2 py-1 text-xs text-ink-muted hover:bg-surface-raised"
          >
            Close
          </button>
        </div>

        <DrawerSection title="Identity">
          <KeyValue label="Candidate" value={String(candidate.candidate_id ?? "—")} />
          <KeyValue label="Stage" value={String(candidate.current_stage ?? "—")} />
          <KeyValue label="Signal time" value={isoClock(rec.settled_candle_timestamp as string)} />
        </DrawerSection>

        <DrawerSection title="Technical">
          <KeyValue
            label="Technical score"
            value={
              typeof candidate.technical_score === "number"
                ? pct(candidate.technical_score * 100, 0)
                : "—"
            }
          />
          <KeyValue
            label="Source"
            value="technical (not ML probability)"
          />
          {Array.isArray(candidate.supporting_strategies) && (
            <KeyValue label="Strategies" value={candidate.supporting_strategies.join(", ") || "—"} />
          )}
        </DrawerSection>

        <DrawerSection title="Multi-timeframe">
          {mtf ? (
            <>
              <KeyValue
                label="Result"
                value={mtf.conflicted ? "MTF_CONFLICT" : "aligned"}
                tone={mtf.conflicted ? "critical" : "good"}
              />
              <KeyValue label="BUY TFs" value={(mtf.buy_timeframes ?? []).join(", ") || "—"} />
              <KeyValue label="SELL TFs" value={(mtf.sell_timeframes ?? []).join(", ") || "—"} />
            </>
          ) : (
            <p className="text-xs text-ink-muted">No multi-timeframe data.</p>
          )}
        </DrawerSection>

        <DrawerSection title="Regime">
          <KeyValue label="Current regime" value={String(candidate.current_regime ?? "—")} />
          <KeyValue label="Policy" value={String(rec.regime_policy ?? "—")} />
        </DrawerSection>

        <DrawerSection title="Eligibility">
          <KeyValue
            label="State"
            value={String(candidate.eligibility_status ?? "—")}
            tone={
              String(candidate.eligibility_status).toUpperCase().includes("APPROVED")
                ? "good"
                : "warning"
            }
          />
          <KeyValue label="Grain" value={`FOREX · ${String(candidate.strategy)} · ${String(candidate.timeframe)}`} />
          {candidate.rejection_reason && (
            <KeyValue label="Reason" value={String(candidate.rejection_reason)} tone="critical" />
          )}
        </DrawerSection>

        <DrawerSection title="ML / Qwen / Validation">
          <KeyValue label="ML" value={String(rec.ml_status ?? "—")} />
          <KeyValue label="Qwen" value={String(rec.qwen_status ?? "—")} />
        </DrawerSection>

        <DrawerSection title="Risk">
          <KeyValue label="Entry" value={fxPrice(entry, symbol)} />
          <KeyValue label="Stop" value={fxPrice(stop, symbol)} />
          <KeyValue label="Target" value={fxPrice(target, symbol)} />
          <KeyValue label="R:R" value={rr ? rr.toFixed(2) : "—"} />
          <KeyValue label="Risk result" value={String(rec.risk_result ?? "—")} />
        </DrawerSection>

        <DrawerSection title="Final">
          <KeyValue
            label="Decision"
            value={String(rec.final_action ?? candidate.decision_status ?? state)}
            tone={String(rec.final_action).includes("PAPER") ? "good" : undefined}
          />
        </DrawerSection>
      </div>
    </div>
  );
}

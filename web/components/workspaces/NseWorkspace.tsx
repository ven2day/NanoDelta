"use client";

import {
  CandlestickChart,
  Gauge,
  History,
  LayoutDashboard,
  BrainCircuit,
  PieChart,
  ShieldCheck,
  TrendingUp,
  Zap,
} from "lucide-react";
import { useState } from "react";
import { AccountPanel } from "@/components/AccountPanel";
import { ActivityLogPanel } from "@/components/ActivityLogPanel";
import { CycleLifecyclePanel } from "@/components/CycleLifecyclePanel";
import { SixLayerLifecycle, type LifecycleLayer } from "@/components/SixLayerLifecycle";
import { KpiRow } from "@/components/KpiRow";
import { ModelLearningStatus } from "@/components/ModelLearningStatus";
import { OpenPositionsPanel } from "@/components/OpenPositionsPanel";
import { PipelinePanel } from "@/components/PipelinePanel";
import { RegimePanel } from "@/components/RegimePanel";
import { ScalpDecisionTable } from "@/components/ScalpDecisionTable";
import { ScalpingCandidatesPanel } from "@/components/ScalpingCandidatesPanel";
import { SectorMoversPanel } from "@/components/SectorMoversPanel";
import { SessionCostPanel } from "@/components/SessionCostPanel";
import { SignalHistoryPanel } from "@/components/SignalHistoryPanel";
import { SystemStatusPanel } from "@/components/SystemStatusPanel";
import { TradeChartsPanel } from "@/components/TradeChartsPanel";
import { TradeHistoryPanel } from "@/components/TradeHistoryPanel";
import { Sidebar } from "@/components/ui/Sidebar";
import type { SignalFunnel, TradingStats } from "@/lib/types";
import type { HistoryPoint } from "@/lib/useTradingState";

const TABS = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "charts", label: "Charts", icon: CandlestickChart },
  { id: "sectors", label: "Sector Movers", icon: PieChart },
  { id: "scalping", label: "Scalping Candidates", icon: Zap },
  { id: "scalp-decisions", label: "All Scalp Signals", icon: Gauge },
  { id: "signals", label: "Signal History", icon: History },
  { id: "trades", label: "Trade History", icon: TrendingUp },
  { id: "status", label: "System Status", icon: ShieldCheck },
  { id: "models", label: "Model / Learning", icon: BrainCircuit },
];

export function NseWorkspace({ stats, history }: { stats: TradingStats; history: HistoryPoint[] }) {
  const [activeTab, setActiveTab] = useState("overview");
  const funnel = stats.signal_funnel as Partial<SignalFunnel>;
  const quoteCount = Object.keys(stats.market_quotes ?? {}).length;
  const strategyEvaluations = funnel.strategy_evaluations ?? 0;
  const finalDecisions = funnel.final_buy === undefined
    ? stats.signals_validated + stats.signals_rejected
    : (funnel.final_buy ?? 0) + (funnel.final_hold ?? 0) + (funnel.final_wait ?? 0) + (funnel.final_reject ?? 0);
  const layers: LifecycleLayer[] = [
    { id: "raw", value: `${quoteCount} quotes`, detail: `${stats.quote_source} provider payloads`, state: quoteCount > 0 ? "active" : "idle" },
    { id: "canonical", value: `${funnel.symbols_ready ?? quoteCount} ready`, detail: `${stats.candle_source} normalized market data`, state: quoteCount > 0 ? "ready" : "idle" },
    { id: "feature", value: `${strategyEvaluations} evaluations`, detail: "Current-cycle features and strategies", state: strategyEvaluations > 0 ? "ready" : "idle" },
    { id: "decision", value: `${finalDecisions} decisions`, detail: "Evidence, AI review and risk gates", state: finalDecisions > 0 ? "ready" : "idle" },
    { id: "execution", value: `${stats.open_positions.length} open`, detail: `${stats.execution_mode} orders and portfolio`, state: stats.open_positions.length > 0 ? "active" : "idle" },
    { id: "outcome", value: `${stats.total_trades} closed`, detail: "Performance, attribution and learning", state: stats.total_trades > 0 ? "ready" : "idle" },
  ];
  return (
    <div className="min-w-0 space-y-4">
      <nav aria-label="NSE workspace sections" className="w-full">
        <Sidebar
          tabs={TABS}
          active={activeTab}
          onChange={setActiveTab}
          orientation="horizontal"
        />
      </nav>
      <div className="min-w-0 space-y-4">
        <CycleLifecyclePanel stats={stats} />
        <SixLayerLifecycle market="NSE" provider={stats.quote_source} layers={layers} />
        <KpiRow stats={stats} history={history} />
        {activeTab === "overview" && (
          <div className="space-y-4">
            <PipelinePanel stats={stats} />
            <div className="columns-1 gap-4 md:columns-2 xl:columns-3 [&>*]:mb-4 [&>*]:break-inside-avoid">
              <ActivityLogPanel stats={stats} />
              <OpenPositionsPanel stats={stats} />
              <SessionCostPanel stats={stats} />
              <RegimePanel stats={stats} />
              <AccountPanel stats={stats} />
            </div>
          </div>
        )}
        {activeTab === "charts" && <TradeChartsPanel stats={stats} />}
        {activeTab === "sectors" && <SectorMoversPanel stats={stats} expanded />}
        {activeTab === "scalping" && <ScalpingCandidatesPanel stats={stats} />}
        {activeTab === "scalp-decisions" && <ScalpDecisionTable stats={stats} />}
        {activeTab === "signals" && <SignalHistoryPanel />}
        {activeTab === "trades" && <TradeHistoryPanel />}
        {activeTab === "status" && <SystemStatusPanel />}
        {activeTab === "models" && <ModelLearningStatus market="NSE" />}
      </div>
    </div>
  );
}

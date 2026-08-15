"use client";

import { Boxes, Cable, ShieldOff } from "lucide-react";
import { SixLayerLifecycle, type LifecycleLayer } from "@/components/SixLayerLifecycle";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";

const LAYERS: LifecycleLayer[] = [
  { id: "raw", value: "No feed", detail: "Exchange payload adapter required", state: "unconfigured" },
  { id: "canonical", value: "Schema ready", detail: "Candles, ticks and books", state: "unconfigured" },
  { id: "feature", value: "Schema ready", detail: "Features, regimes and strategy inputs", state: "unconfigured" },
  { id: "decision", value: "Schema ready", detail: "Evidence and risk decision records", state: "unconfigured" },
  { id: "execution", value: "Orders off", detail: "No exchange execution adapter", state: "blocked" },
  { id: "outcome", value: "Awaiting trades", detail: "Attribution begins after paper closes", state: "idle" },
];

export function CryptoWorkspace() {
  return (
    <section className="space-y-4" aria-label="CRYPTO workspace">
      <div className="rounded-xl border border-border bg-surface px-5 py-4 shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-ink-primary">CRYPTO <span className="text-ink-muted">/ PROVIDER UNCONFIGURED</span></h1>
            <p className="mt-1 text-xs text-ink-muted">Shared architecture is available; runtime and market feed are not registered.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Badge label="RUNTIME: UNCONFIGURED" tone="warning" dot />
            <Badge label="EXECUTION: PAPER ONLY" tone="neutral" />
            <Badge label="EXCHANGE ORDERS: OFF" tone="good" />
          </div>
        </div>
      </div>

      <SixLayerLifecycle market="CRYPTO" provider="No provider" layers={LAYERS} />

      <div className="grid gap-4 md:grid-cols-2">
        <Card title="Architecture ready" icon={Boxes} accent="var(--status-good)">
          <p className="text-xs leading-5">Bronze envelopes, canonical market data, Gold features, evidence journals, paper execution records and outcome lineage share the same contracts used by NSE and Forex.</p>
        </Card>
        <Card title="Runtime required" icon={Cable} accent="var(--status-warning)">
          <p className="text-xs leading-5">Register an exchange data provider, symbol universe, strategy policy and paper execution worker before this workspace can display market metrics.</p>
          <div className="mt-3 flex items-center gap-2 text-xs text-status-warning"><ShieldOff size={13} /> No live or paper orders are being created.</div>
        </Card>
      </div>
    </section>
  );
}

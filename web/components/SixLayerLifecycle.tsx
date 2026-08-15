import {
  BrainCircuit,
  CheckCircle2,
  CircleDashed,
  Database,
  History,
  Layers3,
  LockKeyhole,
  Send,
  ShieldCheck,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type LayerState = "active" | "ready" | "idle" | "blocked" | "unconfigured";

export interface LifecycleLayer {
  id: "raw" | "canonical" | "feature" | "decision" | "execution" | "outcome";
  value: string;
  detail: string;
  state: LayerState;
}

const DEFINITIONS: Record<
  LifecycleLayer["id"],
  { label: string; short: string; icon: LucideIcon; accent: string }
> = {
  raw: { label: "Raw / Bronze", short: "E", icon: Database, accent: "var(--cat-1)" },
  canonical: { label: "Canonical / Silver", short: "T", icon: Layers3, accent: "var(--cat-2)" },
  feature: { label: "Feature / Gold", short: "T", icon: BrainCircuit, accent: "var(--cat-4)" },
  decision: { label: "Decision", short: "T", icon: ShieldCheck, accent: "var(--cat-5)" },
  execution: { label: "Execution", short: "L", icon: Send, accent: "var(--cat-3)" },
  outcome: { label: "Outcome", short: "L", icon: History, accent: "var(--cat-6)" },
};

const STATE = {
  active: { label: "ACTIVE", icon: CircleDashed, color: "var(--status-good)" },
  ready: { label: "READY", icon: CheckCircle2, color: "var(--status-good)" },
  idle: { label: "IDLE", icon: CircleDashed, color: "var(--ink-muted)" },
  blocked: { label: "BLOCKED", icon: LockKeyhole, color: "var(--status-critical)" },
  unconfigured: { label: "UNCONFIGURED", icon: LockKeyhole, color: "var(--status-warning)" },
} satisfies Record<LayerState, { label: string; icon: LucideIcon; color: string }>;

export function SixLayerLifecycle({
  market,
  provider,
  layers,
}: {
  market: string;
  provider: string;
  layers: LifecycleLayer[];
}) {
  return (
    <section className="rounded-xl border border-border bg-surface shadow-card" aria-label={`${market} six-layer lifecycle`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-ink-primary">Six-layer data and trading lifecycle</h2>
          <p className="mt-0.5 text-xs text-ink-muted">Extract → transform → decide → load execution and outcomes</p>
        </div>
        <div className="text-right text-xs">
          <span className="font-semibold text-ink-secondary">{market}</span>
          <span className="ml-2 text-ink-muted">{provider}</span>
        </div>
      </div>
      <div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-6">
        {layers.map((layer, index) => {
          const definition = DEFINITIONS[layer.id];
          const state = STATE[layer.state];
          const Icon = definition.icon;
          const StateIcon = state.icon;
          return (
            <div key={layer.id} className="relative min-w-0 rounded-lg border border-border bg-surface-raised p-3">
              {index < layers.length - 1 && (
                <span aria-hidden="true" className="absolute -right-2 top-1/2 z-10 hidden h-px w-2 bg-border xl:block" />
              )}
              <div className="flex items-start justify-between gap-2">
                <span className="flex h-7 w-7 items-center justify-center rounded-md" style={{ color: definition.accent, background: "rgba(255,255,255,0.04)" }}>
                  <Icon size={15} />
                </span>
                <span className="rounded px-1.5 py-0.5 text-[0.62rem] font-bold tracking-widest text-ink-muted">{definition.short}</span>
              </div>
              <p className="mt-2 text-xs font-semibold text-ink-primary">{definition.label}</p>
              <p className="mt-1 truncate text-base font-semibold tabular-nums" style={{ color: definition.accent }} title={layer.value}>{layer.value}</p>
              <p className="mt-1 min-h-8 text-[0.68rem] leading-4 text-ink-muted">{layer.detail}</p>
              <span className="mt-2 inline-flex items-center gap-1 text-[0.62rem] font-semibold tracking-wide" style={{ color: state.color }}>
                <StateIcon size={11} className={layer.state === "active" ? "animate-spin" : ""} />
                {state.label}
              </span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

"use client";

import { Activity } from "lucide-react";
import { useEffect, useState } from "react";
import { getFinOpsSummary, getMarketsSummary, type MarketName, type MarketStatus } from "@/lib/marketApi";
import type { FinOpsSummary } from "@/lib/types";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";

const MARKETS: MarketName[] = ["NSE", "FOREX", "CRYPTO"];

export function AllMarketsWorkspace() {
  const [summary, setSummary] = useState<Partial<Record<MarketName, MarketStatus>>>({});
  const [finops, setFinops] = useState<FinOpsSummary | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try {
        const [value, llm] = await Promise.all([getMarketsSummary(), getFinOpsSummary()]);
        if (active) { setSummary(value); setFinops(llm); }
      } catch {
        if (active) setSummary({});
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 10_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const totalCalls = finops?.calls ?? 0;
  const candidateCalls = finops?.candidate_review_calls ?? 0;
  const contextCalls = finops?.context_support_calls ?? 0;
  const totalTokens = finops?.total_tokens ?? 0;
  const totalCost = finops?.total_cost_usd ?? 0;
  return (
    <section className="space-y-4" aria-label="All markets summary">
      <div>
        <h1 className="text-2xl font-semibold text-ink-primary">All markets</h1>
        <p className="text-xs text-ink-muted">Aggregate health only. Processing remains inside independent workers.</p>
      </div>
      <div className="grid gap-4 md:grid-cols-3">
        {MARKETS.map((market) => {
          const item = summary[market];
          const unconfigured = market === "CRYPTO" && !item;
          const healthy = item?.status === "HEALTHY";
          return (
            <Card key={market} title={market} icon={Activity} accent={healthy ? "var(--status-good)" : "var(--ink-muted)"}>
              <div className="flex items-center justify-between">
                <Badge label={item?.status ?? (unconfigured ? "UNCONFIGURED" : "UNAVAILABLE")} tone={healthy ? "good" : unconfigured ? "warning" : "neutral"} dot />
                <span className="text-xs text-ink-muted">{item?.provider ?? (unconfigured ? "No provider" : "-")}</span>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
                <span>BUY {item?.buy_count ?? 0}</span><span>SELL {item?.sell_count ?? 0}</span>
              </div>
              {unconfigured && <p className="mt-3 border-t border-border pt-3 text-xs text-ink-muted">Schemas ready; runtime and exchange adapter required.</p>}
            </Card>
          );
        })}
      </div>
      <Card title="LLM Today — All Markets" icon={Activity} accent="var(--cat-4)">
        <div className="grid grid-cols-2 gap-2 text-xs md:grid-cols-5">
          <span>Total calls {totalCalls}</span><span>Candidate {candidateCalls}</span>
          <span>Context/support {contextCalls}</span><span>Tokens {totalTokens.toLocaleString()}</span>
          <span>Cost ${totalCost.toFixed(4)}</span>
        </div>
      </Card>
    </section>
  );
}

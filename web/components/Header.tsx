"use client";

import { ArrowLeftRight, Coins, Grid2X2, IndianRupee, LogOut, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { logout } from "@/lib/api";
import type { TradingStats } from "@/lib/types";
import { Badge } from "./ui/Badge";
import { Tabs } from "./ui/Tabs";

const MARKETS = [
  { id: "all", label: "All Markets", icon: Grid2X2 },
  { id: "nse", label: "NSE", icon: IndianRupee },
  { id: "forex", label: "Forex", icon: ArrowLeftRight },
  { id: "crypto", label: "Crypto", icon: Coins },
];

// Just a ticking IST clock for display — whether new entries are actually allowed
// comes from the backend (stats.market_open), which is the same is_trading_window()/
// force_trading_window check the trading loop itself gates on. Guessing that
// independently here risked disagreeing with what the system was really doing (e.g.
// showing "NSE CLOSED" while force_trading_window had the loop actively trading).
function useISTClock(): string {
  const [clock, setClock] = useState("--:--:-- IST");
  useEffect(() => {
    const tick = () => {
      const ist = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Kolkata" }));
      setClock(`${ist.toTimeString().slice(0, 8)} IST`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return clock;
}

export function Header({
  stats,
  connected,
  activeMarket,
  onMarketChange,
}: {
  stats: TradingStats | null;
  connected: boolean;
  activeMarket: string;
  onMarketChange: (id: string) => void;
}) {
  const clock = useISTClock();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    router.replace("/login");
  }

  const showNseState = activeMarket === "nse";
  const mode = stats?.trading_mode ?? "paper";
  const quoteSource = stats?.quote_source ?? stats?.data_source ?? "simulated";
  const dataLabel = ["dhan", "dhan_rest"].includes(quoteSource)
    ? "DHAN LIVE"
    : quoteSource.replaceAll("_", " ").toUpperCase();
  const brokerOrdersEnabled = stats?.broker_orders_enabled ?? false;
  const marketOpen = stats?.market_open ?? false;
  const forced = stats?.force_trading_window ?? false;

  return (
    <div className="col-span-full grid grid-cols-1 gap-3 border-b border-border pb-4 sm:grid-cols-[auto_1fr_auto] sm:items-center">
      <div className="flex items-center gap-2.5">
        <ShieldCheck size={22} className="text-cat-1" strokeWidth={2} />
        <div>
          <div className="text-base font-semibold leading-tight text-ink-primary">
            ₹DeltaQuant
          </div>
          <div className="text-xs text-ink-muted">Multi-market paper trading</div>
        </div>
      </div>

      <div className="flex justify-center">
        <Tabs tabs={MARKETS} active={activeMarket} onChange={onMarketChange} />
      </div>

      <div className="flex flex-wrap items-center justify-end gap-2">
        {showNseState && <Badge label={`EXECUTION: ${mode.toUpperCase()}`} tone={mode === "paper" ? "good" : "critical"} />}
        {showNseState && <Badge label={`DATA: ${dataLabel}`} tone={quoteSource === "simulated" ? "warning" : "good"} dot />}
        {showNseState && <Badge label={`BROKER ORDERS: ${brokerOrdersEnabled ? "ON" : "OFF"}`} tone={brokerOrdersEnabled ? "critical" : "good"} />}
        {showNseState && <Badge label={forced ? "MARKET: FORCED TEST WINDOW" : marketOpen ? "MARKET: OPEN" : "MARKET: CLOSED"} tone={forced ? "critical" : marketOpen ? "good" : "neutral"} dot />}
        {showNseState && <Badge
          label={connected ? "NSE DASHBOARD: CONNECTED" : "NSE DASHBOARD: DISCONNECTED"}
          tone={connected ? "good" : "critical"}
          dot
          pulse={connected}
        />}
        {showNseState && <span className="tabular text-xs text-ink-muted">{clock}</span>}
        <button
          type="button"
          onClick={handleLogout}
          title="Sign out"
          className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-ink-muted transition-colors hover:border-status-critical hover:text-status-critical"
        >
          <LogOut size={12} strokeWidth={2} />
          Sign out
        </button>
      </div>
    </div>
  );
}

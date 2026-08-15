"use client";

import { useState } from "react";
import { Header } from "@/components/Header";
import { AllMarketsWorkspace } from "@/components/workspaces/AllMarketsWorkspace";
import { CryptoWorkspace } from "@/components/workspaces/CryptoWorkspace";
import { ForexWorkspace } from "@/components/workspaces/ForexWorkspace";
import { NseWorkspace } from "@/components/workspaces/NseWorkspace";
import { useAuthGuard } from "@/lib/useAuthGuard";
import { useTradingState } from "@/lib/useTradingState";

export default function Home() {
  const { checking } = useAuthGuard();
  const { state, connected, history } = useTradingState();
  const [activeMarket, setActiveMarket] = useState("all");

  if (checking) {
    return <main className="mx-auto max-w-7xl p-6 text-center text-ink-muted">Checking session...</main>;
  }

  return (
    <main className="mx-auto flex max-w-[96rem] flex-col gap-5 p-4 sm:p-6">
      <Header
        stats={state}
        connected={connected}
        activeMarket={activeMarket}
        onMarketChange={setActiveMarket}
      />
      {activeMarket === "all" && <AllMarketsWorkspace />}
      {activeMarket === "nse" && (
        state ? <NseWorkspace stats={state} history={history} /> : (
          <div className="py-24 text-center italic text-ink-muted">Waiting for the NSE worker...</div>
        )
      )}
      {activeMarket === "forex" && <ForexWorkspace />}
      {activeMarket === "crypto" && <CryptoWorkspace />}
    </main>
  );
}

import { apiFetch } from "./api";
import type { FinOpsSummary } from "./types";

export type MarketName = "NSE" | "FOREX" | "CRYPTO";

export interface MarketStatus {
  market: MarketName;
  status: string;
  provider?: string;
  environment?: string;
  execution?: string;
  data?: string;
  market_data?: string;
  pricing_stream?: string;
  runtime_id?: string;
  generated_at?: string | null;
  age_seconds?: number | null;
  writer_runtime_id?: string;
  health_state?: string;
  snapshot_publication?: string;
  dashboard_connectivity?: string;
  buy_count?: number;
  sell_count?: number;
  llm_finops?: FinOpsSummary;
  llm_session?: FinOpsSummary;
  [key: string]: unknown;
}

export interface MarketDecision {
  market: MarketName;
  symbol?: string;
  instrument?: string;
  timeframe?: string;
  side?: string;
  signal_type?: string;
  strategy?: string;
  confidence?: number;
  entry_price?: number;
  stop_loss?: number;
  target_price?: number;
  timestamp?: string;
  [key: string]: unknown;
}

export interface MarketTechnicalCandidate extends MarketDecision {
  candidate_id?: string;
  candidate_version?: string;
  current_stage?: string;
  decision_status?: string;
  rejection_reason?: string | null;
  eligibility_status?: string;
  current_regime?: string;
  regime_policy?: string;
  entry_quality?: string;
  technical_score?: number | null;
  supporting_strategies?: string[];
  multi_timeframe?: {
    conflicted?: boolean;
    buy_timeframes?: string[];
    sell_timeframes?: string[];
  };
}

export interface MarketStrategyStatus {
  market: MarketName;
  strategy_name?: string;
  strategy?: string;
  timeframe?: string;
  eligibility?: string;
  validation_status?: string;
  ml_policy?: string;
  [key: string]: unknown;
}

export interface MarketModelStatus {
  market: MarketName;
  strategy_name?: string;
  timeframe?: string;
  model_version?: string;
  status?: string;
  trained_at?: string;
  training_data_until?: string;
  challenger_status?: string;
  [key: string]: unknown;
}

async function json<T>(path: string): Promise<T> {
  const response = await apiFetch(path);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

export function getMarketStatus(market: MarketName): Promise<MarketStatus> {
  return json<MarketStatus>(`/api/${market.toLowerCase()}/status`);
}

export function getMarketSignals(market: MarketName): Promise<MarketDecision[]> {
  return json<MarketDecision[]>(`/api/${market.toLowerCase()}/signals?days=7`);
}

export function getMarketCandidates(market: MarketName): Promise<MarketTechnicalCandidate[]> {
  return json<MarketTechnicalCandidate[]>(`/api/${market.toLowerCase()}/candidates`);
}

export function getMarketPositions(market: MarketName): Promise<MarketDecision[]> {
  return json<MarketDecision[]>(`/api/${market.toLowerCase()}/positions`);
}

export function getMarketStrategies(market: MarketName): Promise<MarketStrategyStatus[]> {
  return json<MarketStrategyStatus[]>(`/api/${market.toLowerCase()}/strategies`);
}

export function getMarketModels(market: MarketName): Promise<MarketModelStatus[]> {
  return json<MarketModelStatus[]>(`/api/${market.toLowerCase()}/models`);
}

export function getMarketsSummary(): Promise<Record<MarketName, MarketStatus>> {
  return json<Record<MarketName, MarketStatus>>("/api/markets/summary");
}

export function getFinOpsSummary(market?: MarketName): Promise<FinOpsSummary> {
  const query = market ? `?market=${market}` : "";
  return json<FinOpsSummary>(`/api/finops/summary${query}`);
}

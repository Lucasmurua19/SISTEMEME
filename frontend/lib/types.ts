// Contratos de la API (los enteros grandes llegan como cadenas).

export type CandidateStatus = "observing" | "pending_data" | "eligible" | "rejected" | "expired";

export interface Reason {
  code: string;
  message: string;
  data?: Record<string, unknown>;
}

export interface Candidate {
  mint: string;
  symbol: string | null;
  name: string | null;
  pool: string;
  status: CandidateStatus;
  reasons: Reason[];
  score: string | null;
  liquidity_lamports: string | null;
  round_trip_cost_pct: string | null;
  window_index: number;
  evaluated_at: string | null;
  migrated_at: string | null;
  age_s: number;
  data_age_s: number | null;
  migration_signature: string;
  verified: boolean;
  canonical: boolean | null;
  signal: { id: number; status: string; issued_at: string | null; expires_at: string | null; invalidation_level: string | null } | null;
  positions: Record<string, string>;
}

export interface CandidateList {
  as_of: string;
  strategy_version: { id: number; name: string } | null;
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
  items: Candidate[];
  no_entry: { message: string; tracked: number; reasons: { code: string; count: number }[] } | null;
}

export interface SourceHealth {
  source: string;
  stream: string;
  degraded: boolean;
  reason: string | null;
  last_healthy_at: string | null;
  consecutive_failures: number;
  last_error: string | null;
  cursor: string | null;
  last_slot: number | null;
}

export interface SystemStatus {
  overall: "ok" | "degraded" | "down";
  as_of: string;
  data_mode: string;
  worker: { alive: boolean; last_heartbeat_at: string | null; silence_s: number | null };
  ingest_lag_s: number | null;
  last_slot: number | null;
  strategy: { id: number; name: string; fingerprint: string } | null;
  sources: SourceHealth[];
  database: string;
}

export interface SnapshotPoint {
  window_index: number;
  window_end: string;
  observed_at: string;
  open: string;
  high: string;
  low: string;
  close: string;
  liquidity_lamports: string;
  swap_count: number;
  net_buyers: number;
  buy_volume_lamports: string;
  sell_volume_lamports: string;
  top10_concentration_pct: string | null;
  coverage: string;
}

export interface FilterRow {
  name: string;
  code: string;
  outcome: "pass" | "fail" | "pending" | "skipped";
  message: string;
  value: string | null;
  threshold: string | null;
}

export interface Level {
  price: string;
  window_index: number;
  at: string;
  snapshot_id: number | null;
}

export interface Pattern {
  state: string;
  detail: string;
  peak: Level | null;
  low: Level | null;
  drawdown_pct: string | null;
  recovery_pct: string | null;
  net_buyers: number | null;
  liquidity_retention_pct: string | null;
  invalidation_level: string | null;
  confirmed_window_index: number | null;
  confirmed_at: string | null;
  invalidated_window_index: number | null;
}

export interface ScoreComponent {
  name: string;
  value: string;
  score: string;
  weight: string;
  points: string;
}

export interface RoundTrip {
  total_debited_lamports: string;
  net_recoverable_lamports: string;
  cost_lamports: string;
  cost_pct: string;
  buy_in_lamports: string;
  buy_extra_costs_lamports: string;
  sell_out_lamports: string;
  sell_extra_costs_lamports: string;
  refunds_lamports: string;
  immobilized_lamports: string;
  tokens_expected: string;
  tokens_min: string;
}

export interface Evaluation {
  id: number;
  window_index: number;
  status: CandidateStatus;
  recorded_status: CandidateStatus;
  reasons: Reason[];
  filters: FilterRow[];
  metrics: Record<string, string>;
  pattern: Pattern;
  score: string | null;
  score_components: ScoreComponent[];
  round_trip: RoundTrip | null;
  as_of: string;
  evaluated_at: string;
  snapshot_refs: number[];
}

export interface QuoteRow {
  id: number;
  purpose: string;
  side: string;
  status: string;
  in_amount: string;
  out_amount: string;
  min_out_amount: string;
  route: string;
  price_impact_bps: string;
  costs: { kind: string; lamports: string; included_in_amount: boolean; refundable?: boolean }[];
  error: string | null;
  observed_at: string;
  expires_at: string;
}

export interface Attempt {
  id: number;
  kind: "entry" | "exit";
  number: number;
  status: string;
  scheduled_at: string;
  executed_at: string | null;
  trigger: string | null;
  failure_cause: string | null;
  requested_amount: string | null;
  result_amount: string | null;
  detail: Record<string, unknown>;
}

export interface Position {
  id: number;
  session_id: number;
  policy: string | null;
  signal_id: number;
  mint: string;
  symbol: string | null;
  pool: string;
  status: "pending_entry" | "open" | "closed" | "rejected";
  reject_reason: string | null;
  reject_detail: Record<string, unknown> | null;
  tokens_expected: string;
  tokens_min: string;
  debited_lamports: string;
  reserved_lamports: string;
  immobilized_lamports: string;
  entry_price: string | null;
  opened_at: string | null;
  closed_at: string | null;
  exit_trigger: string | null;
  exit_failures: number;
  illiquid: boolean;
  next_exit_at: string | null;
  last_recoverable_lamports: string | null;
  last_recoverable_adverse_lamports: string | null;
  last_valued_at: string | null;
  valuation_status: string | null;
  proceeds_lamports: string | null;
  adverse_proceeds_lamports: string | null;
  pnl_lamports: string | null;
  zero_recovery_pnl_lamports: string | null;
  sol_usd_open: string | null;
  sol_usd_close: string | null;
  attempts: Attempt[];
}

export interface PoolDetail {
  address: string;
  program: string;
  quote_mint: string;
  migrated_at: string;
  migration: { signature: string; instruction_index: number; slot: number };
  verified: boolean;
  identity_ok: boolean;
  canonical: boolean | null;
  verification: Record<string, unknown>;
  snapshots: SnapshotPoint[];
  evaluation: Evaluation | null;
  history: { window_index: number; status: CandidateStatus; score: string | null; as_of: string }[];
  quotes: QuoteRow[];
  signal: {
    id: number;
    status: string;
    issued_at: string | null;
    expires_at: string | null;
    score: string;
    round_trip_cost_pct: string;
    invalidation_level: string;
    entry_ref_price: string;
  } | null;
  positions: Position[];
}

export interface TokenDetail {
  mint: string;
  symbol: string | null;
  name: string | null;
  decimals: number;
  as_of: string;
  pools: PoolDetail[];
}

export interface SessionCapital {
  capital_lamports: string;
  cash_lamports: string;
  reserved_lamports: string;
  available_lamports: string;
  open_cost_basis_lamports: string;
  realized_pnl_lamports: string;
  open_deficit_lamports: string;
  net_session_pnl_lamports: string;
  active_positions: number;
}

export interface SessionInfo {
  id: number;
  name: string;
  policy: "ranking" | "liquidity";
  strategy_version_id: number;
  paused: boolean;
  pause_reason: string | null;
  started_at: string;
  limits: {
    trade_size_lamports: string;
    max_exposure_lamports: string;
    max_positions: number;
    max_session_loss_lamports: string;
  };
  capital: SessionCapital;
}

export interface PaperTrades {
  session: SessionInfo;
  positions: Position[];
}

export interface Metrics {
  trades_closed: number;
  trades_open: number;
  trades_illiquid: number;
  realized_pnl_lamports: string;
  realized_pnl_sol: string;
  realized_pnl_usd: string | null;
  /** Abiertas con ruta de salida, al mínimo garantizado y después de costos (las ilíquidas no entran acá). */
  open_valuation_lamports: string;
  illiquid_cost_basis_lamports: string;
  /** Todas las abiertas: con ruta al mínimo garantizado; sin ruta, recuperación cero. */
  open_pnl_valued_lamports: string;
  /** Igual, pero las que tienen ruta, al precio cotizado: cota optimista. */
  open_pnl_quoted_lamports: string;
  /** Realizado + abiertas valoradas. */
  total_pnl_valued_lamports: string;
  open_oldest_valuation_at: string | null;
  zero_recovery_scenario_pnl_lamports: string;
  expectancy_lamports: string | null;
  hit_rate: string | null;
  avg_win_lamports: string | null;
  avg_loss_lamports: string | null;
  profit_factor: string | null;
  /** Solo curva de operaciones cerradas; no incluye la pérdida latente de las abiertas. */
  max_realized_drawdown_lamports: string;
  max_realized_drawdown_pct_of_capital: string | null;
  pnl_without_best_lamports: string | null;
  best_trade_lamports: string | null;
  adverse_pnl_lamports: string;
  failed_exit_attempts: number;
  exit_attempts_total: number;
  failed_exit_rate: string | null;
  positions_with_exit_trigger: number;
  positions_with_failed_exit: number;
  entries_rejected: number;
  no_entry_reasons: { code: string; count: number }[];
}

export interface MetricsResponse {
  session: SessionInfo;
  window: { since: string | null; until: string | null };
  metrics: Metrics;
  comparison: {
    reference_session: { id: number; name: string; policy: string };
    reference: Metrics;
    delta: Record<string, string>;
    note: string;
  } | null;
  fx: { pair: string; rate: string; observed_at: string; source: string } | null;
  disclaimer: string;
}

export interface OpsResponse {
  as_of: string;
  ingestion_lag_s: number | null;
  stale_snapshot_pools: number;
  jobs: { by_status: Record<string, number>; retried: number };
  quotes: { by_status: Record<string, number>; failures: number };
  candidates_discarded_by_reason: { code: string; count: number }[];
  exit_attempts: { total: number; failed: number };
  reserved_capital_lamports: string;
}

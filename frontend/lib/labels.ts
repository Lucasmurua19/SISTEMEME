// Etiquetas en español de estados, filtros y códigos de motivo (los códigos son estables en la API).

export const STATUS_LABEL: Record<string, string> = {
  eligible: "Elegible",
  observing: "Observando",
  pending_data: "Pendiente de datos",
  rejected: "Descartado",
  expired: "Vencido",
};

export const STATUS_HELP: Record<string, string> = {
  eligible: "Pasó todos los filtros: candidato evaluable.",
  observing: "El patrón todavía se está formando.",
  pending_data: "Falta un dato crítico o está desactualizado.",
  rejected: "No cumple un filtro obligatorio.",
  expired: "Perdió vigencia (edad, invalidación o señal vencida).",
};

export const REASON_LABEL: Record<string, string> = {
  MIGRATION_UNVERIFIED: "Migración no verificada",
  IDENTITY_MISMATCH: "Mint y pool no coinciden",
  UNSUPPORTED_QUOTE_MINT: "Pool cotizado en otro token (no SOL)",
  NO_MARKET_DATA: "Sin datos de mercado",
  STALE_MARKET_DATA: "Datos de mercado vencidos",
  LOW_DATA_COVERAGE: "Cobertura de datos insuficiente",
  CONCENTRATION_UNKNOWN: "Concentración desconocida",
  CONCENTRATION_TOO_HIGH: "Concentración alta",
  PATTERN_IN_PROGRESS: "Patrón en formación",
  RECOVERY_NOT_CONFIRMED: "Recuperación sin confirmar",
  DRAWDOWN_TOO_DEEP: "Caída excesiva",
  PATTERN_INVALIDATED: "Patrón invalidado",
  PATTERN_EXPIRED: "Edad máxima superada",
  INSUFFICIENT_NET_BUYERS: "Pocos compradores netos",
  INSUFFICIENT_DEPTH: "Profundidad insuficiente",
  LIQUIDITY_DETERIORATING: "Liquidez en deterioro",
  QUOTE_PENDING: "Cotización pendiente",
  NO_BUY_ROUTE: "Sin ruta de compra",
  NO_SELL_ROUTE: "Sin ruta de venta",
  QUOTE_ERROR: "Error del proveedor de cotizaciones",
  QUOTE_STALE: "Cotización vencida",
  ROUND_TRIP_COST_EXCEEDED: "Costo de ida y vuelta excesivo",
  SIGNAL_EXPIRED: "Señal vencida",
  SESSION_PAUSED: "Sesión en pausa",
  SESSION_LIMIT: "Límite de pérdida de sesión",
  POSITION_LIMIT: "Máximo de posiciones",
  EXPOSURE_LIMIT: "Límite de exposición",
  INSUFFICIENT_CAPITAL: "Capital insuficiente",
  SOURCE_DEGRADED: "Fuente degradada",
  ENTRY_QUOTE_UNAVAILABLE: "Cotización de entrada no disponible",
  SLIPPAGE_EXCEEDED: "Slippage excedido",
  EXIT_INVALIDATION: "Salida por invalidación",
  EXIT_LIQUIDITY: "Salida por deterioro de liquidez",
  EXIT_TAKE_PROFIT: "Salida por objetivo",
  EXIT_MAX_HOLD: "Salida por tiempo máximo",
  EXIT_FAILED: "Salida fallida",
};

export const FILTER_LABEL: Record<string, string> = {
  migration_verified: "Migración verificada",
  identity: "Identidad mint–pool",
  age: "Edad desde la migración",
  market_data: "Datos frescos y completos",
  concentration: "Concentración de holders",
  pattern: "Patrón caída → recuperación",
  recovery_holding: "Recuperación vigente",
  net_buyers: "Compradores netos distintos",
  depth: "Profundidad para el tamaño",
  liquidity_retention: "Estabilidad de la liquidez",
  buy_route: "Ruta de compra",
  sell_route: "Ruta de venta",
  quote_fresh: "Cotización vigente",
  round_trip_cost: "Costo de ida y vuelta",
};

export const COMPONENT_LABEL: Record<string, string> = {
  recovery: "Recuperación",
  liquidity: "Estabilidad de liquidez",
  cost: "Costo de salida",
  holders: "Distribución de holders",
};

export const POSITION_LABEL: Record<string, string> = {
  pending_entry: "Entrada pendiente",
  open: "Abierta",
  closed: "Cerrada",
  rejected: "Sin entrada",
};

export const POLICY_LABEL: Record<string, string> = {
  ranking: "Ranking por puntaje",
  liquidity: "Referencia por liquidez",
};

export const ATTEMPT_STATUS_LABEL: Record<string, string> = {
  pending: "Pendiente",
  applied: "Aplicado",
  failed: "Fallido",
  rejected: "Rechazado",
};

export const QUOTE_PURPOSE_LABEL: Record<string, string> = {
  evaluation: "Evaluación",
  entry: "Entrada",
  exit: "Salida",
  valuation: "Valoración",
};

export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? code;
}

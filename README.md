# Radar de memecoins recién migradas en Solana

Detecta migraciones de **Pump.fun → PumpSwap**, evalúa la *primera caída con recuperación*, muestra candidatos con su evidencia y mide el resultado de las señales con una **cartera simulada**. Sin candidatos o sin presupuesto, el panel dice **sin entrada** y explica por qué.

- Especificación funcional: [CONTENIDO.md](CONTENIDO.md) · Arquitectura: [ARQUITECTURA.md](ARQUITECTURA.md)
- **Solo lectura on-chain.** Ningún componente firma ni envía transacciones. Las operaciones son asientos internos del simulador.
- Los resultados simulados **no son evidencia de rentabilidad**. El modo `fixtures` usa datos sintéticos y los umbrales no están calibrados con datos reales.

```mermaid
flowchart LR
    RPC["Solana RPC / WebSocket"] --> W["Worker (Python)"]
    JUP["Jupiter · cotizaciones"] --> W
    W --> DB[("PostgreSQL")]
    API["FastAPI"] --> DB
    UI["Next.js"] -->|"/api/v1/* (proxy)"| API
```

## Arranque rápido

### Con Docker (recomendado)

```powershell
copy .env.example .env      # cambiá POSTGRES_PASSWORD; el resto sirve para fixtures
docker compose up --build
```

Abrí <http://localhost:3000>. Orden de arranque: `db` (healthcheck) → `migrate` (`alembic upgrade head`) → `worker` y `api` → `web`. En modo `fixtures` el worker reproduce 15 escenarios en ~2 minutos: verás candidatos elegibles aparecer, entradas simuladas, salidas por objetivo/invalidación, reintentos de ventas fallidas y una posición ilíquida. Documentación interactiva de la API: <http://localhost:8000/docs>.

### Sin Docker (PowerShell, SQLite)

```powershell
cd backend
py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
$env:DATABASE_URL = "sqlite:///radar.db"
python -m app.cli migrate        # aplica Alembic
python -m app.cli replay         # reproduce todos los escenarios sin esperas y resume el resultado
python -m app.worker             # terminal 2: mantiene vivo el worker
python -m uvicorn app.api.main:app --port 8000   # terminal 3 (con el mismo DATABASE_URL)

cd ..\frontend                   # terminal 4
npm install
$env:API_INTERNAL_URL = "http://127.0.0.1:8000"
npm run dev                      # http://localhost:3000
```

### Pruebas

```powershell
cd backend
pytest                      # todo (146 pruebas)
pytest tests/unit           # dominio y adaptadores, sin base de datos
pytest tests/postgres       # PostgreSQL real: pip install ".[pgtest]" o TEST_DATABASE_URL=postgresql+psycopg://...
cd ..\frontend; npm run typecheck
```

## Modos de datos

| `DATA_MODE` | Fuente | Para qué |
|---|---|---|
| `fixtures` (por defecto) | Escenarios JSON deterministas de `backend/fixtures/scenarios`, reloj de reproducción persistido | Desarrollo, demos y pruebas reproducibles |
| `live` | Solana RPC + WebSocket, PumpSwap y Jupiter Swap v2 (`GET /order`, solo cotización) | Observación real, solo lectura (no hay claves ni firma de transacciones). Requiere `SOLANA_RPC_URL`; `JUPITER_API_KEY` es opcional (más cupo) |

Antes de evaluar señales en vivo, mirá el indicador de estado del panel: una fuente degradada (sin lecturas sanas o con fallas seguidas) **bloquea nuevas entradas** pero mantiene el seguimiento de posiciones abiertas.

**Reglas del modo live.** Por defecto usa `backend/config/strategy-live-v1.json`: los valores por defecto del código, fijados *antes* de ver resultados reales y **sin calibrar con datos**. La versión se guarda con su huella; cambiar un umbral exige un archivo con otro nombre (`STRATEGY_CONFIG_PATH`), nunca editar ese. Así las sesiones de ranking y de liquidez se comparan bajo las mismas reglas congeladas.

**Qué RPC usar.** El RPC público de Solana (`api.mainnet-beta.solana.com`) sirve para una prueba de humo y el worker lo avisa en el log. Limita la tasa (HTTP 429 con `Retry-After`; `getTokenLargestAccounts` en particular) y no sostiene varios pools calientes a la vez. Para observar de forma continua usá un proveedor con cupo propio: sin él, la concentración de holders queda desconocida (el filtro queda pendiente, nunca se inventa) y la lectura de swaps va por detrás de los pools más activos. Si `SOLANA_RPC_URL` conserva el valor de ejemplo de `.env.example`, el worker no arranca y lo dice.

**El ciclo del worker está acotado en tiempo** (`INGEST_BUDGET_S`, `POLL_BUDGET_S`, `EVALUATION_BUDGET_S`): con un RPC lento, lo que no alcanza se retoma en el ciclo siguiente en vez de bloquear el latido, los trabajos y el seguimiento de posiciones abiertas. El log registra el tiempo de cada paso y avisa cuando uno es lento.

## Cómo decide el sistema

1. **Ingesta.** Se detecta la instrucción `migrate` de Pump.fun, se valida programa, mint y pool contra la cuenta real en PumpSwap y se guarda con firma, índice de instrucción, slot y los tres instantes (`event_at`, `observed_at`, `evaluated_at`). El checkpoint avanza en la misma transacción que los eventos.
2. **Patrón.** Máximo inicial en una ventana configurable → caída entre un mínimo y un máximo admisibles → recuperación confirmada con ventanas cerradas consecutivas. Al confirmar, máximo, mínimo y nivel de invalidación **quedan congelados**: ventanas futuras no reescriben la señal.
3. **Filtros obligatorios** (cada uno deja motivo, valor y umbral): migración verificada, identidad mint–pool, edad, datos frescos, concentración evaluable y aceptable, patrón confirmado, compradores netos, profundidad, estabilidad de liquidez, ruta de compra y venta, cotización vigente y costo de ida y vuelta.
4. **Puntaje** 0–100 sin redondear: recuperación 40 % · liquidez 30 % · costo 20 % · holders 10 %, con rangos fijos por versión (no depende de otros candidatos). Desempate por costo y por frescura de la cotización.
5. **Simulación.** Reserva de capital al admitir la señal → entrada con cotización nueva tras el retraso (rechazo registrado si cambió precio, ruta o costo) → valoración conservadora → salida por invalidación, deterioro de liquidez, objetivo o tiempo máximo. Una venta fallida **deja la posición abierta**, registra la causa y reintenta con más tolerancia de slippage; una posición sin ruta se muestra aparte con escenario de recuperación cero.
6. **Evaluación.** Dos sesiones con el mismo universo, presupuesto, retrasos y reglas: *ranking por puntaje* contra *referencia por liquidez*. PnL neto en SOL y USD, expectativa, tasa de acierto, caída máxima del PnL realizado, PnL sin la mejor operación, escenario adverso, posiciones con salida fallida y bloque cronológico (`since`/`until`).

   **Cómo leer el resultado de una sesión.** El panel muestra tres cifras con el mismo peso: *PnL realizado* (solo ventas ya ejecutadas), *Abiertas valoradas* y *Total valorado* (la suma). Las abiertas se valoran de forma conservadora: las que tienen ruta de salida, vendidas al mínimo garantizado y después de costos; las que no tienen ruta, a recuperación cero (pérdida del monto entero). Debajo se informa también la cota optimista (a precio cotizado) y la hora de la valoración más vieja. La *caída máxima del PnL realizado* se calcula solo con operaciones cerradas, por eso no incluye la pérdida latente de las abiertas; para ver ese riesgo mirá *Abiertas valoradas*. (Los documentos de especificación la llaman «caída máxima del capital»: el nombre se cambió a propósito para que no sugiera algo que el cálculo no hace.)

Estados de candidato: `observing`, `pending_data`, `eligible`, `rejected`, `expired`. Los códigos de motivo (`NO_SELL_ROUTE`, `STALE_MARKET_DATA`, `SESSION_LIMIT`, …) son estables y están en [reasons.py](backend/app/domain/reasons.py).

### Configuración versionada

Cada versión fija ventanas, umbrales, montos, costos máximos, caducidades y pesos ([config.py](backend/app/domain/config.py)). Es **inmutable**: reutilizar un nombre con otro contenido falla; para cambiar umbrales creá un archivo nuevo (p. ej. `backend/config/mi-estrategia-v2.json`) y apuntá `STRATEGY_CONFIG_PATH`. Los valores de [strategy-fixtures-v1.json](backend/config/strategy-fixtures-v1.json) son de fixtures: calibrá con observaciones reales y congelalos antes de la evaluación fuera de muestra.

## API

| Ruta | Contenido |
|---|---|
| `GET /api/v1/candidates?status=eligible&limit=25` | Ranking paginado, `as_of`, versión y `no_entry` con motivos cuando no hay elegibles |
| `GET /api/v1/tokens/{mint}` | Pools, serie de snapshots, cotizaciones y evidencia de la última evaluación |
| `GET /api/v1/paper-trades?session_id=` | Posiciones, intentos, costos, PnL y estado de salida |
| `GET /api/v1/metrics?session_id=&since=&until=` | Métricas y comparación con la referencia por liquidez |
| `GET /api/v1/signals`, `/sessions`, `/ops`, `/status` | Señales para depurar, sesiones, indicadores operativos, estado de fuentes |
| `GET /api/v1/export/{paper-trades,candidates}.csv` | Exportaciones (protegidas contra inyección de fórmulas) |
| `POST /api/v1/sessions/{id}/pause\|resume` | Pausa/reanuda nuevas entradas; exige `X-Admin-Token` |
| `GET /health/live`, `/health/ready` | Vida del proceso; preparación (base, latido del worker, fuentes degradadas) |

Los lamports y unidades base viajan como **cadenas** (sin pérdida de precisión en JSON).

## Seguridad y operación

- Claves RPC/Jupiter solo en el servidor; los logs redactan claves y credenciales en URLs. El navegador solo habla con `/api/v1/*` del panel (proxy que valida rutas y solo reenvía `GET`, más pausa/reanudación si `PANEL_CONTROLS=true`; el token de administración se agrega en el servidor).
- Puertos publicados solo en `127.0.0.1` (panel 3000, API 8000). **La base no se publica**: la alcanzan la API y el worker por la red interna. Para usar `psql` o un cliente gráfico: `docker compose -f compose.yaml -f compose.db-port.yaml up` (puerto 55432, para no chocar con un PostgreSQL instalado en Windows). **Si exponés el panel a internet**, ponele autenticación delante (proxy inverso con login), restringí `CORS_ORIGINS` y revisá `RATE_LIMIT_PER_MINUTE`.
- Datos externos (nombres, símbolos) se sanean en la API y React los escapa; el panel no carga imágenes ni URLs de tokens y la CSP bloquea orígenes de terceros.
- Reintentos con espera creciente, trabajos con reclamo vencible (`FOR UPDATE SKIP LOCKED`) y pausa persistida: sobreviven reinicios. Escalado en vivo: `docker compose up --scale worker=2`. En `fixtures` dejá un solo worker.
- Operación: `GET /api/v1/ops` expone retraso de ingesta, snapshots vencidos, trabajos reintentados, fallas de cotización, descartes por motivo, intentos de salida y capital reservado. Auditoría de cambios de estado en la tabla `audit_log`.

## Casos de aceptación ↔ pruebas

| Caso (CONTENIDO.md §7) | Prueba |
|---|---|
| Migración real → candidato visible; dos reprocesamientos, una evaluación por ventana | `test_replay_pipeline::TestIdempotency` |
| Desconexión y recuperación sin duplicados | `test_live_adapters::TestMigrationSource` (cursor, orden, huecos) + `TestIdempotency::test_one_migration_reprocessed…` |
| Cotización vencida | `test_strategy::test_stale_quote_is_pending`, `test_risk_simulation::test_expired_signal_stale_quote…` |
| Ausencia de ruta de venta | `test_strategy::test_no_sell_route_is_pending…`, `TestFullReplay` (ZETA) |
| Concentración desconocida | `test_strategy::test_unknown_concentration…`, `test_live_adapters` (propietario sin resolver) |
| Saltos de precio | `test_risk_simulation::test_price_jump…`, `TestFullReplay::test_price_jump…` (IOTA) |
| Venta fallida mantiene la posición abierta | `TestFullReplay` (THET, OMIC, KAPA) |
| Límites simultáneos con dos workers | `tests/postgres::test_two_workers_never_open_the_same_signal_twice` |
| Rebote sin volumen, datos incompletos | `test_strategy::TestEvaluate` |

## Estructura

```text
backend/app/domain/          estrategia, riesgo, costos, simulación, métricas (puro, sin I/O)
backend/app/application/     casos de uso y contratos (ports.py)
backend/app/infrastructure/  PostgreSQL/SQLAlchemy, RPC de Solana, Jupiter, fixtures, jobs
backend/app/api/             FastAPI        backend/app/worker.py   ciclo del worker
backend/migrations/          Alembic        backend/fixtures/       escenarios sintéticos
backend/tests/               unit · integration · postgres
frontend/                    Next.js (ranking, detalle de token, simulación)
compose.yaml  .env.example
```

## Verificación realizada y límites conocidos

**Verificado en este entorno**
- 187 pruebas automáticas: reglas de dominio, flujo completo con fixtures (migración → señal → posición → PnL), API, migraciones (`upgrade`, `check` sin diferencias y `downgrade`), **PostgreSQL real** (tipos `NUMERIC(40,0)`/`JSONB`, enteros u64 exactos, cuatro workers compitiendo por las mismas señales, reclamos con `SKIP LOCKED`) y **transacciones reales de mainnet** capturadas como fixtures (`backend/tests/fixtures/solana_real/`).
- Frontend: chequeo de tipos, build de producción y revisión visual con capturas (tema oscuro, claro y 500 px de ancho).
- **`docker compose up --build` en Windows con Docker 26:** las imágenes se construyen, `migrate` aplica Alembic sobre PostgreSQL 16, y `db`, `api` y `web` quedan `healthy`. La reproducción de fixtures sobre ese PostgreSQL dio el mismo resultado exacto que las pruebas con SQLite.
- **Modo `live` en solo lectura contra Solana y Jupiter reales** (2026-11-25, RPC público de Solana y Jupiter sin clave, base SQLite descartable). Se siguió una migración real de punta a punta con los adaptadores del sistema: firma → evento de migración → verificación on-chain del pool (cuenta real en PumpSwap, PDA canónica) → snapshots de mercado por ventanas → cotización de compra y de salida en Jupiter. Durante unos 12 minutos el worker detectó 13 migraciones reales por WebSocket y por reconciliación, y registró una evaluación real. Esa prueba encontró y corrigió defectos que las respuestas sintéticas no mostraban:
  - Las transacciones versión 1 se rechazaban (`maxSupportedTransactionVersion`).
  - Casi la mitad del tráfico de la cuenta de migración son reintentos de bots sobre una curva ya migrada («Bonding curve already migrated»); el `migrate` viejo los tomaba por migraciones nuevas. Ahora una migración exige el `create_pool` real de PumpSwap.
  - Jupiter responde `HTTP 400 «Failed to get quotes»` cuando no hay ruta; se clasificaba como error de la fuente en lugar de «sin ruta».
  - El WebSocket no verificaba el certificado en Windows; usa ahora el mismo bundle que `httpx`.
  - Un ciclo del worker se bloqueaba 55–96 s esperando al RPC (sin latido ni seguimiento de posiciones): ahora hay topes de tiempo, latido entre pools, concentración sin reintentos y con retroceso, y lectura concurrente.
  - Hay migraciones legítimas cuyo pool cotiza en **otro token de Pump.fun** y no en SOL (1 de 13); se rechazan con su propio motivo, `UNSUPPORTED_QUOTE_MINT`, sin gastar cotizaciones.

**No verificado (decílo antes de confiar en ello)**
- **Operación sostenida en vivo.** La prueba duró minutos con el RPC público, que limita la tasa: no se vio cómo se comporta durante horas o días ni con un proveedor de cupo propio.
- **Ninguna migración real llegó a señal ni a posición simulada.** No es una conclusión sobre la estrategia: con el RPC público la concentración de holders queda desconocida (el filtro queda pendiente), el sondeo de swaps no alcanzó a los pools calientes y las reservas iniciales observadas fueron muy dispares (0,14 SOL, 0,29 SOL y 70,9 SOL en las tres que se midieron, contra los 20 SOL que exige el filtro de profundidad). Por eso la cotización de salida se verificó con los adaptadores sobre un pool real, pero **no a través de una evaluación completa del worker**.
- Latencia de ingesta: entre el bloque y el registro pasaron entre 1,6 y 30 s (mediana ≈ 11 s). Los trabajos disparados por WebSocket esperan al paso de evaluación del ciclo; un RPC con más cupo la reduce.
- Los umbrales de `live-v1` no están calibrados y los resultados de fixtures son sintéticos: ninguno es evidencia de rentabilidad.
- **Comparar Ranking y Liquidez durante varias sesiones todavía no es posible:** hay un único par de sesiones por versión de estrategia, y una sesión que alcanza su pérdida máxima queda pausada para siempre. Hace falta agregar «corridas» de sesión (par de sesiones nuevo por período, con las mismas reglas).

## Decisiones que complementan los documentos

- Se guarda una evaluación por pool, ventana y versión, siempre sobre la **última** ventana cerrada. Si cierran varias entre dos ciclos solo se evalúa la última (no se puede cotizar en un instante pasado), pero el patrón reproduce todas las ventanas. Vencimientos que dependen del tiempo (edad máxima, señal vencida, datos obsoletos) se resuelven al leer, sin reescribir evaluaciones guardadas.
- Compradores netos con confirmación de varias ventanas: se usa el máximo por ventana (cota inferior de wallets distintas).
- Los depósitos de cuentas de token (reembolsables) se registran por separado y no cuentan como costo; sí cuentan como fondos inmovilizados (y como recuperación cero en posiciones ilíquidas).
- Las cotizaciones de valoración de posiciones abiertas no se persisten salvo que fallen (evidencia de fallas); las de evaluación, entrada y salida sí.
- Las salidas fallidas se reintentan sin tope de intentos, con tolerancia de slippage creciente hasta un máximo; tras `exit_retry_max` fallos la posición se marca ilíquida.
- Actualización del panel por *polling* configurable (`POLL_SECONDS`), como pide la arquitectura para el primer corte.

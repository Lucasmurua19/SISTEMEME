# Bot de entrada y salida para SISTEMEME

Guía para implementar ejecución de swaps de SOL y memecoins de Solana sobre el radar existente. El radar detecta candidatos y aplica la estrategia. El ejecutor comprueba los límites, prepara y envía la operación autorizada, y verifica cuánto se compró o vendió en cadena.

## 1. Base revisada

Revisión del commit [31fb8d3 · mejoras1.2](https://github.com/Lucasmurua19/SISTEMEME/commit/31fb8d3).

| Archivo existente | Qué resuelve hoy | Cómo aprovecharlo |
|---|---|---|
| `backend/app/application/evaluate_pool.py` | Evalúa el candidato y emite señales. | Consumir la señal con su versión, ventana, tamaño y vencimiento. |
| `backend/app/application/process_position.py` | Simula entradas y salidas, aplica límites, registra tiempos y reintenta ventas. Agrupa entradas por señal e instante programado. | Reusar los cálculos puros de decisión y riesgo. El ejecutor real necesita su propio estado de orden y confirmación. |
| `backend/app/infrastructure/jupiter_quotes.py` | Obtiene cotizaciones de Jupiter y distingue errores de falta de ruta. | Mantenerlo para evaluar candidatos. Agregar un adaptador de órdenes firmables y ejecución. |
| `backend/app/worker.py` y `infrastructure/jobs.py` | Procesan trabajos persistidos y registran tiempos de ciclo. | Agregar un consumidor de ejecución con su propia cadencia y medir la espera desde el instante programado. |
| `scripts/medir_live.py` | Resume la corrida y sus tiempos. | Extender el informe con aprobación, firma, envío, confirmación y costos reales. |

El commit agrupa la decisión de entrada simulada. La consulta de red ocurre antes de tomar los bloqueos de base, así que dos workers todavía pueden pedir cotizaciones en paralelo aunque el resultado se aplique una sola vez. Para dinero real, reclamá **una intención persistida antes de preparar y firmar**. La salida simulada sigue consultándose por posición.

## 2. Modos de ejecución

| Modo propuesto | Comportamiento |
|---|---|
| `paper` | Usa las sesiones actuales de Ranking y Liquidez, con fondos simulados. Es el modo inicial. |
| `manual` | El bot detecta y prepara la orden. El usuario revisa importe, token, costo y mínimo a recibir, y firma desde su billetera. Si caduca, se prepara otra y se pide una aprobación nueva. |
| `auto` | Un servicio de firma aislado autoriza órdenes dentro de la política configurada. Empieza con una billetera dedicada y una posición simultánea. |

`DATA_MODE` define la fuente de mercado. `EXECUTION_MODE` define el destino de la orden. Usá `DATA_MODE=live` con `EXECUTION_MODE=paper` para simular con mercado real. El arranque debe rechazar cualquier combinación de fixtures con ejecución real. Elegí **una sola política activa por billetera real** y mantené Ranking y Liquidez en paper para compararlas. Compartir una cotización simulada entre políticas no implica abrir dos operaciones reales.

## 3. Compra

1. Al vencer el retraso de la señal, reclamá la intención por billetera y señal en PostgreSQL. Revisá vigencia, saldo, reserva para comisiones, posición máxima, pérdida de sesión y salud de las fuentes.
2. Cotizá la compra para el monto configurado y la venta de la cantidad mínima esperada. Cancelá si no hay ruta de salida, la señal venció o el costo y el impacto exceden los límites.
3. Solicitá a Jupiter Swap v2 una orden ejecutable con el `taker` de la billetera operadora. Validá mints, monto, firmante, cuentas de destino, mínimo a recibir, cargos y vigencia contra la intención. Si la última cotización queda fuera de autorización, cancelá o pedí una nueva aprobación.
4. Firmá y persistí el intento, el `requestId`, la transacción firmada bajo acceso restringido y los datos de vigencia **antes** de enviarla con `POST /execute`. Para la reconciliación, guardá el identificador on-chain cuando esté disponible. Si la ruta agrega una firma externa y no podés conocer el txid antes del envío, implementá una reconciliación verificable para esa ruta antes de habilitarla en automático.
5. Confirmá en Solana. Guardá los cambios de saldo atribuibles a la transacción y las comisiones observadas. Abrí la posición real por la cantidad acreditada. Una cotización o un HTTP 200 por sí solos dejan la orden pendiente.

[Jupiter Swap v2 · Order & Execute](https://developers.jup.ag/docs/swap/order-and-execute) · [Solana · getSignatureStatuses](https://solana.com/docs/rpc/http/getsignaturestatuses) · [Solana · getTransaction](https://solana.com/docs/rpc/http/gettransaction)

## 4. Venta

- Reusá los disparadores del radar: objetivo, invalidación, deterioro de liquidez y tiempo máximo. Guardá cuál disparó la orden y cuándo.
- Calculá el tamaño con el **saldo confirmado de la posición**, descontando cualquier venta pendiente. Solicitá una cotización nueva y usá el mínimo autorizado para ese intento. El precio que disparó la salida queda como referencia de auditoría.
- Una orden pendiente de confirmación bloquea una segunda venta de esa posición. Si falla de forma confirmada, conservá la posición, actualizá el valor recuperable y programá un reintento con espera creciente y un presupuesto de intentos y comisiones. Al agotarlo, marcá `needs_attention` y mantené el seguimiento.
- Si no hay ruta, el panel muestra **«Sin ruta de venta»** y el riesgo usa la valoración conservadora configurada. Un tiempo máximo o un stop disparan un intento de vender. La ejecución depende del mercado y de que exista ruta.
- Cerrá la posición solo tras reconciliar la venta. Calculá el PnL con los flujos reales en SOL, las comisiones y cualquier depósito de cuenta efectivamente recuperado. Registrá cantidades residuales por separado.

## 5. Persistencia y recuperación

| Objeto nuevo | Campos mínimos |
|---|---|
| `execution_intents` | Billetera, señal o posición, lado, monto, modo, política, vencimiento, estado y clave de idempotencia. |
| `execution_attempts` | Intención, número, cotización, `requestId`, firma o identificador de seguimiento, vigencia, estado, tiempos y causa de error. |
| `live_positions` | Billetera, mint, importe acreditado, SOL debitado, saldo abierto, ventas confirmadas y valoración fechada. |
| `live_ledger` | Reserva, débito, crédito, comisión y depósito de cuenta, con referencia única al movimiento confirmado. |

Estados de intento sugeridos: `prepared → signed → submitted → confirmed`. Salidas alternativas: `rejected`, `failed`, `expired` y `unknown`.

- **Exclusión.** Una compra activa por billetera y señal, y una venta activa por posición. Reservá saldo y creá la intención en una transacción de base. Usá reclamación con vencimiento y unicidad para impedir que dos workers procesen la misma orden. Mantené las llamadas externas fuera de los bloqueos de base.
- **Timeout o reinicio.** Si se desconoce el resultado del envío, conciliá la firma, vigencia y estado on-chain antes de crear una orden de reemplazo. Un estado desconocido mantiene la reserva y bloquea una nueva orden sobre esos fondos.
- **Conciliación.** Al confirmar, contabilizá una vez el movimiento real. Si hay discrepancia de saldo o falta metadata, pausá nuevas entradas de esa billetera y dejá el intento pendiente de conciliación.
- **Tiempo.** Guardá señal, turno, petición y recepción de cotización. Agregá firma, envío y confirmación. Mostrá mediana y percentil 95 para distinguir cola del worker, tiempo de Jupiter y tiempo on-chain.

## 6. Módulos, configuración y panel

```text
backend/app/application/execution/   # Preparar, aprobar, ejecutar y conciliar
backend/app/domain/execution.py      # Estados, política y límites
backend/app/infrastructure/         # Adaptador Jupiter y almacenamiento de órdenes
backend/migrations/versions/        # Migración Alembic de ejecución
frontend/app/execution/             # Intenciones, confirmaciones, PnL y pausa
```

| Configuración propuesta | Comportamiento inicial |
|---|---|
| `EXECUTION_MODE` | `paper`. El cambio a real es explícito. |
| `EXECUTION_POLICY` | Política autorizada para esa billetera, con versión fija. |
| `EXECUTION_MAX_ORDER_LAMPORTS` | Tope de importe configurado explícitamente para real. |
| `EXECUTION_MAX_OPEN_POSITIONS` | Una posición en la primera prueba real. |
| `EXECUTION_MAX_SESSION_LOSS_LAMPORTS` | Pausa nuevas entradas al alcanzar el límite, incluyendo la valoración conservadora de abiertas. |
| `EXECUTION_MAX_ORDER_AGE_S` y límites de impacto, slippage y comisiones | Rechazan órdenes fuera de la política vigente. |

La clave de firma vive fuera del frontend, los logs y Git. Para `auto`, dale acceso solo al servicio de firma de la billetera operadora. El panel debe permitir **pausar compras y seguir gestionando salidas**. Mostrá modo, billetera pública, saldo, reservas, orden pendiente, firma on-chain y diferencia entre PnL estimado y confirmado. Protegé las acciones de aprobación, pausa y reanudación con autenticación y auditoría.

## 7. Implementación y pruebas

- [ ] **Primero, ejecutor falso.** Implementá estados, tareas y conciliación con respuestas controladas. Probá señal vencida, falta de ruta, orden fuera de límites y doble worker.
- [ ] **Después, preparación en vivo.** Pedí órdenes ejecutables sin firmar y medí si seguirían vigentes al momento en que se habría enviado la operación. Compará sus cotizaciones con las de paper.
- [ ] **Después, firma manual.** Con una billetera dedicada y presupuesto explícito, verificá una compra y una venta confirmadas. Compará montos, costos y tiempos contra la simulación.
- [ ] **Antes de activar automático.** Probá timeout después del envío, caída antes y después de persistir, recuperación al reiniciar, deslizamiento, posiciones ilíquidas, límite de pérdidas y pausa. Verificá que nunca se aplique dos veces el mismo movimiento confirmado.
- [ ] **Evaluación.** Conservá la comparación paper con entradas y salidas equivalentes. Contrastá resultados confirmados con el modelo y corré varias sesiones con la misma versión antes de ampliar el capital. El PnL simulado de una hora sirve para depurar el proceso.

**Primer entregable de código:** con un proveedor falso, una señal de fixtures genera una intención, atraviesa compra y venta, y deja trazabilidad completa incluso si el proceso se reinicia a mitad del envío.

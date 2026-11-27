# Radar de memecoins recién migradas en Solana

Sistema para detectar migraciones de Pump.fun a PumpSwap, ordenar candidatos según una estrategia explícita y medir si las señales producen ganancia neta con costos y retrasos realistas. El resultado del radar es un **candidato evaluable** o **sin entrada**. La puntuación indica qué tan bien cumple las reglas y solo un ensayo posterior puede estimar su rentabilidad.

**Estado del proyecto** · Este README es la especificación de desarrollo. El MVP trabaja con datos públicos, cotizaciones de solo lectura y operaciones simuladas.

## 1. Estrategia inicial

Seguir tokens recién migrados y evaluar **la primera caída con recuperación después de la migración**. El momento de la señal empieza cuando la recuperación ya puede confirmarse con datos observados. Si la señal llega tarde o la liquidez se deteriora, vence.

- **GMGN y Axiom** sirven para contrastar si detectaste el mismo token y a qué hora apareció. **Axiom y Pump Terminal** sirven como referencias para comparar la ejecución manual. La ruta y las tarifas de esas terminales pueden diferir de la cotización de Jupiter.
- La identidad del activo es su **mint**. Guardá también la dirección del pool. El símbolo solo sirve para mostrarlo.
- Una aparición en Binance Alpha se puede registrar como dato de contexto. La estrategia inicial evalúa su salida en el mercado on-chain. [Binance Alpha](https://www.binance.com/es-LA/support/faq/detail/60cc2e54aa32453387523c10254438c1)

## 2. Los seis parámetros

| Parámetro | Implementación | Decisión |
|---|---|---|
| 1. Liquidez y antigüedad | Confirmá la migración y el pool de PumpSwap. Medí edad, reservas, liquidez y evolución en la ventana observada. | Descartá pools sin profundidad suficiente para el tamaño evaluado, con liquidez en deterioro o datos vencidos. |
| 2. Concentración y wallets | Sumá balances por propietario, separá pools y cuentas de quema verificadas y calculá concentración de los 10 mayores sobre el suministro analizado. Revisá compras tempranas, transferencias y fondeo común. | Un dato crítico incompleto deja el candidato pendiente. Una concentración alta puede excluirlo. Las coincidencias de fondeo se muestran como indicios, con sus transacciones y nivel de confianza. |
| 3. Primera caída y recuperación | Usá ventanas cerradas de swaps y estados consecutivos: migración, máximo observado, caída, recuperación y señal. Contá compradores netos distintos y medí si se sostuvo la liquidez. | Una recuperación confirmada habilita la evaluación. Si vuelve a caer bajo el nivel de invalidación, vence el candidato. |
| 4. Costo real de salida | Cotizá SOL → token y después token → SOL con las unidades que devolvería la compra. Registrá ruta, montos, impacto, costos, momento y vencimiento. | Sin ruta de venta, con cotización vencida o con costo sobre el límite, descartá la entrada. |
| 5. Riesgo de la sesión | Configurá tamaño por operación, exposición total, máximo de posiciones y pérdida máxima de sesión. Reservá capital al abrir una posición y evaluá el déficit de las abiertas con cotizaciones de salida conservadoras. | Al superar un límite, bloqueá nuevas posiciones simuladas. Seguí actualizando las abiertas y registrando señales. |
| 6. Registro y validación | Guardá el conjunto completo de candidatos, decisiones, señales, cotizaciones, aperturas, cierres, fallos y versión de estrategia. | Compará PnL neto contra una regla simple y revisá si la ventaja persiste con datos nuevos. |

### Reglas de la señal

1. Identificá el máximo inicial dentro de una ventana configurable desde la migración.
2. Medí la caída desde ese máximo. Fijá un mínimo de retroceso y un máximo admisible antes de buscar recuperación.
3. Medí cuánto recuperó desde el mínimo, cuántas wallets distintas compraron en términos netos y si la liquidez se mantiene durante la confirmación.
4. Registrá el máximo, el mínimo y los datos que confirmaron el rebote **en ese momento**. Conservar esos puntos evita que velas futuras reescriban una señal anterior.
5. Consultá cotizaciones y verificá filtros y límites justo antes de publicar la señal. Caducá el candidato cuando pierda vigencia.

### Filtros y ranking

Primero hay filtros obligatorios: migración verificada, identidad del mint y pool, datos frescos, concentración evaluable, recuperación confirmada y salida cotizable dentro del costo máximo. Registrá el motivo de cada descarte, aunque el token luego se dispare.

Ordená los elegibles con métricas normalizadas de 0 a 100. Para la primera versión usá pesos orientativos: **40% recuperación, 30% estabilidad de liquidez, 20% costo de salida y 10% distribución de holders**. Un costo menor y una concentración menor suman puntos. Usá rangos fijos por versión para que el puntaje de un token no cambie solo porque aparecieron otros candidatos. Desempatá por costo de salida y luego por la frescura de la cotización.

Si los filtros no dejan candidatos elegibles o se agotó el presupuesto de riesgo, el panel muestra **sin entrada** con los motivos. Guardá el puntaje y las métricas sin redondear para poder reproducir el ranking.

## 3. Cotizaciones y PnL

- Usá enteros para lamports y unidades mínimas de tokens. Guardá precios derivados con precisión decimal. Convertí a USD con una referencia SOL/USD fechada y conservá ambos resultados.
- El costo de ida y vuelta es `(SOL total debitado - SOL neto recuperable) / SOL total debitado`. Sumá únicamente cargos que la cotización todavía no incorporó. Registrá por separado comisión de protocolo o ruta, comisión de la interfaz si corresponde, prioridad, propina y depósitos de cuentas de token que inmovilicen fondos.
- Ejemplo simulado: debitás 0,100 SOL y la venta inmediata devolvería 0,096 SOL netos. El costo de ida y vuelta es **4%**. Si más tarde cerrás recuperando 0,108 SOL netos en total, el PnL realizado es **+0,008 SOL**.
- Una cotización es una estimación en un instante. Para simular ejecuciones, usá una nueva cotización después del retraso configurado. Registrá el impacto estimado y tratá el slippage permitido como límite de ejecución, con un escenario adverso separado.
- Configurá las salidas antes de evaluar resultados: invalidación del patrón, objetivo opcional, deterioro de liquidez y tiempo máximo en posición. Si un disparador ocurre y la venta falla, la posición sigue abierta. Reintentá según el modelo de simulación y registrá el fallo. Para una posición ilíquida, mostrala por separado y calculá un escenario conservador de recuperación cero.
- Un stop es un disparador de intento de salida. El precio conseguido depende de la ruta y de la liquidez disponible.

## 4. Arquitectura

| Componente | Implementación propuesta |
|---|---|
| Worker de ingesta | Python. Suscripción por WebSocket a eventos relevantes, lectura de transacciones y reconciliación por RPC después de una desconexión. |
| Fuentes | Solana RPC e indexador opcional para eventos, swaps, titulares y transferencias. PumpSwap para validar los datos del pool. Jupiter para cotizaciones de solo lectura. |
| Motor de señales | Funciones de Python que reciben una instantánea fechada y una versión de configuración, y devuelven filtros, métricas, puntaje y motivos. |
| Simulador | Worker que consume señales, aplica el presupuesto de riesgo y usa cotizaciones posteriores para estimar aperturas y cierres. |
| Persistencia | PostgreSQL, SQLAlchemy y migraciones con Alembic. |
| API y pantalla | FastAPI para consultas. Next.js para ranking, detalle del token, historial de simulación y estado de las fuentes. |
| Desarrollo local | Docker Compose para base, worker, API y frontend. Primero permití correr con fixtures reproducibles y luego conectá las fuentes reales. |

Flujo: **evento de migración → validación del pool → snapshots → filtros → ranking → cotización posterior → operación simulada → PnL**.

### Integraciones

- Identificá la migración mediante las cuentas e instrucciones del programa publicado. Validá que el mint y el pool coincidan con la migración observada y distinguí un pool canónico de otro par con el mismo token. [Documentación pública de Pump.fun](https://github.com/pump-fun/pump-public-docs)
- En Solana usá la suscripción para recibir eventos, guardá el último punto procesado y completá huecos con RPC. Registrá slot, firma, instante del evento, instante de recepción y nivel de confirmación. Exigí la confirmación elegida antes de habilitar señales. [RPC de Solana](https://solana.com/docs/rpc)
- Para la cotización, usá el endpoint de órdenes de solo lectura de Swap v2 y conservá los campos necesarios de la respuesta. Puede devolver una transacción preparada según la solicitud. El MVP solo consume la cotización. Si una ruta acaba de aparecer y el agregador todavía no la conoce, mantené el candidato pendiente hasta que exista una cotización fresca. [Jupiter Swap API](https://developers.jup.ag/docs/swap)
- Guardá el RPC y las claves de proveedor del lado del servidor. Aplicá límites y reintentos con espera creciente ante errores transitorios. Marcá como degradada cualquier fuente que supere su antigüedad máxima o falle repetidamente.

### Estructura propuesta

```text
backend/
  app/
    api/
    ingestion/
    strategy/
    simulation/
    vendors/
    db/
  migrations/
  tests/
frontend/
  app/
  components/
compose.yaml
.env.example
README.md
```

### Datos y API mínima

| Entidad | Campos principales |
|---|---|
| `pools` y `chain_events` | Mint, pool, programa, firma, índice de instrucción, slot, instante del evento, instante de recepción. |
| `market_snapshots` | Pool, liquidez, precio, volumen, concentración, fuente y timestamps. |
| `quotes` | Mints, entrada, salida, costos, ruta, fecha de toma y vigencia. |
| `signals` | Versión de estrategia, datos observados, puntaje, estado, motivos y evidencia. |
| `paper_positions` y `paper_fills` | Sesión, señal, cantidad, SOL debitado y recibido, intentos, costos y estado. |

- `GET /api/candidates` · Candidatos, elegibilidad, antigüedad de datos y ranking.
- `GET /api/tokens/{mint}` · Pools, señales y evidencia de cada decisión.
- `GET /api/paper-trades` · Posiciones, intentos y PnL.
- `GET /api/metrics` · Rendimiento por versión de estrategia y comparación con referencia.
- `GET /health` · Último slot, retraso de ingesta y disponibilidad de cotizaciones.

Al reintentar la ingesta, deduplicá por firma e índice de instrucción o evento. Para abrir una posición simulada, reservá el presupuesto y registrá la posición en la misma transacción de PostgreSQL. Impedí que dos workers abran dos veces la misma señal dentro de una sesión.

### Configuración inicial

Definí una configuración versionada con: tamaño en SOL, exposición máxima, pérdida máxima de sesión, edad máxima tras migración, ventana del patrón, retroceso mínimo y máximo, confirmación de recuperación, mínimo de compradores netos, concentración máxima, profundidad mínima, tope de costo de ida y vuelta, vigencia de datos y cotizaciones, retrasos simulados y tiempo máximo de posición. Usá valores de fixtures para empezar. Calibrá umbrales con observaciones reales y congelalos antes de la evaluación fuera de muestra.

## 5. Seguridad operativa

- El MVP usa un adaptador de cotizaciones y una cartera simulada. Los secretos de RPC y proveedores viven en variables del servidor. El archivo `.env.example` contiene nombres y valores de ejemplo.
- Autenticá el panel si lo exponés a internet. Aplicá validación de entradas y límites por ruta. Mostrá nombres, imágenes y URLs de tokens como datos externos y escapá su contenido.
- Cuando un dato crítico esté incompleto, desactualizado o en conflicto, bloqueá la señal y mostrá la causa. Al reiniciar, recuperá el estado y conciliá posiciones antes de admitir nuevas entradas.
- Registrá cada cambio de configuración y cada transición de estado. Un interruptor del simulador pausa nuevas entradas y preserva el seguimiento de posiciones abiertas.
- Si más adelante probás órdenes reales, tratá esa integración como una fase independiente: billetera de operación separada, firma manual, revisión de la transacción, límites y conciliación con el resultado on-chain.

## 6. Cómo validar si hay ventaja

1. **Primeras 30 señales para depuración.** Verificá tiempos, duplicados, cotizaciones y si el algoritmo detectó el patrón con datos que ya estaban disponibles. Incluí señales elegibles que después vencieron o quedaron sin salida.
2. **Evaluación cronológica.** Ajustá con un período inicial, congelá la versión y medí en un bloque posterior. Con datos históricos sin cotizaciones reales podés reconstruir escenarios, indicando qué costos y rutas fueron estimados. Usá simulación en vivo para confirmar el comportamiento.
3. **Referencia.** Compará el ranking completo con una selección por liquidez sobre el mismo universo elegible, en instantes equivalentes, con iguales tamaños, presupuesto, retrasos y reglas de salida. Si disponés de otra terminal como referencia, compará el SOL neto recibido para el mismo mint y monto.
4. **Métricas.** Mostrá PnL neto en SOL y USD, expectativa por operación, tasa de acierto, ganancia/pérdida promedio, caída máxima del capital, frecuencia de salidas fallidas y PnL sin la mejor operación. Mostrá cuántas señales terminan sin entrada y por qué.
5. **Escenarios adversos.** Repetí con más retraso, mayor costo y peores salidas. Si el resultado positivo desaparece al retirar un solo trade o depende de vender sin una ruta observada, seguí en simulación.

Como ejercicio manual, elegí wallets rentables y contrastá el precio de sus compras con el que habrías obtenido al detectarlas. Separá ganancias provenientes de swaps observados de tokens recibidos por transferencia. Documentá los vínculos de wallets como indicios. [Estudio sobre grupos de compradores tempranos](https://arxiv.org/abs/2610.39458)

## 7. Orden de desarrollo

- [ ] **Datos y reproducción.** Crear servicios locales, migraciones y fixtures. Guardar una migración real y verificar que dos reprocesamientos producen un solo evento.
- [ ] **Monitor.** Detectar pools, calcular snapshots y mostrar un panel con último slot, estado de fuentes y antigüedad de datos.
- [ ] **Filtros y ranking.** Implementar los filtros obligatorios, una estrategia versionada y un detalle que explique cada puntaje y cada descarte.
- [ ] **Cotizaciones y simulación.** Estimar ida y vuelta, integrar capital simulado, retrasos, reglas de salida y fallos de cotización.
- [ ] **Evaluación.** Habilitar comparación contra liquidez, exportar resultados a CSV y ejecutar la prueba cronológica.
- [ ] **Casos de aceptación.** Validar desconexión y recuperación, cotización vencida, ausencia de ruta de venta, concentración desconocida, saltos de precio, venta fallida, deduplicación y límites simultáneos con dos workers.

**El MVP queda listo cuando una migración real aparece en el panel, cada decisión se puede reproducir con sus datos fechados y el informe muestra el PnL neto frente a la referencia, incluyendo los casos en los que no hubo salida.**

import { CandidatesBoard } from "@/components/CandidatesBoard";
import { api, safe } from "@/lib/api";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const { data, error } = await safe(() => api.candidates("all", 200));
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Radar de migraciones</h1>
          <p>
            Pump.fun → PumpSwap: primera caída con recuperación. El resultado es un <strong>candidato evaluable</strong> o{" "}
            <strong>sin entrada</strong>; el puntaje mide el cumplimiento de las reglas, no la rentabilidad.
          </p>
        </div>
      </div>
      {error && !data && (
        <div className="banner" role="alert">
          <strong>La API no está disponible.</strong> {error}. El panel reintentará automáticamente.
        </div>
      )}
      <CandidatesBoard initial={data} />
    </>
  );
}

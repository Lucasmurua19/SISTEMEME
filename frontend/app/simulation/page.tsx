import { SimulationBoard } from "@/components/SimulationBoard";
import { api, safe } from "@/lib/api";

export const dynamic = "force-dynamic";
export const metadata = { title: "Simulación" };

export default async function SimulationPage() {
  const { data: sessions, error } = await safe(api.sessions);
  const ranking = sessions?.find((s) => s.policy === "ranking") ?? sessions?.[0];
  const { data: metrics } = ranking ? await safe(() => api.metrics(ranking.id)) : { data: null };
  // Los controles de pausa solo aparecen si el servidor del panel los habilitó (PANEL_CONTROLS) y tiene token.
  const controls = process.env.PANEL_CONTROLS === "true" && !!process.env.ADMIN_TOKEN;
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Simulación</h1>
          <p>
            Cartera simulada: el ranking por puntaje frente a una referencia simple por liquidez, con el mismo universo, presupuesto,
            retrasos y reglas de salida.
          </p>
        </div>
      </div>
      {error && !sessions ? (
        <div className="banner" role="alert">
          <strong>La API no está disponible.</strong> {error}
        </div>
      ) : (
        <SimulationBoard sessions={sessions ?? []} controls={controls} initialMetrics={metrics} />
      )}
    </>
  );
}

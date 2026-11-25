"use client";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="banner" role="alert" style={{ marginTop: 24 }}>
      <strong>Algo salió mal al mostrar esta pantalla.</strong>
      <p className="muted" style={{ margin: "4px 0 8px" }}>
        {error.digest ? `Referencia: ${error.digest}` : "Se registró el error en el servidor."}
      </p>
      <button type="button" className="btn" onClick={reset}>
        Reintentar
      </button>
    </div>
  );
}

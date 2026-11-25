import Link from "next/link";

export default function NotFound() {
  return (
    <div className="card" style={{ marginTop: 24 }}>
      <h1>No encontrado</h1>
      <p className="muted" style={{ margin: "6px 0 12px" }}>
        Ese token no fue observado por el radar, o la dirección no es válida.
      </p>
      <Link href="/" className="btn">
        Volver al radar
      </Link>
    </div>
  );
}

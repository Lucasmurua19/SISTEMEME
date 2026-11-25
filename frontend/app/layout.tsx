import type { Metadata } from "next";
import Link from "next/link";
import { Nav } from "@/components/Nav";
import { SystemStatus } from "@/components/SystemStatus";
import { ThemeToggle } from "@/components/ThemeToggle";
import { api, safe } from "@/lib/api";
import { PollingProvider } from "@/lib/usePolling";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Radar de memecoins · Solana", template: "%s · Radar Solana" },
  description: "Migraciones de Pump.fun a PumpSwap, candidatos con evidencia y resultados de una cartera simulada.",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

// Aplica el tema guardado antes del primer pintado para evitar el destello.
const THEME_SCRIPT = `try{var t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const pollSeconds = Number(process.env.POLL_SECONDS ?? "5") || 5;
  const { data: status } = await safe(api.status);
  return (
    <html lang="es">
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <PollingProvider seconds={pollSeconds}>
          <header className="topbar">
            <div className="topbar-inner">
              <Link href="/" className="brand">
                <svg className="brand-mark" viewBox="0 0 24 24" fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="9" opacity="0.35" />
                  <circle cx="12" cy="12" r="4.5" opacity="0.6" />
                  <path d="M12 12L19 7" />
                </svg>
                Radar Solana
              </Link>
              <Nav />
              <span className="spacer" />
              <SystemStatus initial={status} />
              <ThemeToggle />
            </div>
          </header>
          <main className="shell">{children}</main>
          <footer className="shell footer">
            Solo lectura on-chain y operaciones simuladas: ningún componente firma ni envía transacciones. Los resultados
            simulados no son evidencia de rentabilidad real; los datos del modo fixtures son sintéticos.
          </footer>
        </PollingProvider>
      </body>
    </html>
  );
}

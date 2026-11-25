import Link from "next/link";
import { notFound } from "next/navigation";
import { TokenView } from "@/components/TokenView";
import { ApiError, api } from "@/lib/api";
import type { TokenDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

const MINT = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;

export async function generateMetadata({ params }: { params: Promise<{ mint: string }> }) {
  const { mint } = await params;
  return { title: `Token ${mint.slice(0, 6)}…` };
}

export default async function TokenPage({ params }: { params: Promise<{ mint: string }> }) {
  const { mint } = await params;
  if (!MINT.test(mint)) notFound();
  let initial: TokenDetail | null = null;
  try {
    initial = await api.token(mint);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    // otro fallo: el cliente reintenta con polling y muestra el aviso
  }
  return (
    <>
      <p style={{ paddingTop: 16 }}>
        <Link href="/" className="muted">
          ← Volver al radar
        </Link>
      </p>
      <TokenView mint={mint} initial={initial} />
    </>
  );
}

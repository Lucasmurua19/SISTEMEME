"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";

const PollingContext = createContext<number>(5000);

/** El intervalo se configura en el servidor (POLL_SECONDS) y se entrega a los componentes del cliente. */
export function PollingProvider({ seconds, children }: { seconds: number; children: React.ReactNode }) {
  return <PollingContext.Provider value={Math.max(1, seconds) * 1000}>{children}</PollingContext.Provider>;
}

export function usePollInterval(): number {
  return useContext(PollingContext);
}

export interface PollState<T> {
  data: T | null;
  error: string | null;
  /** true mientras se recarga: la pantalla conserva el último dato (sin parpadeo ni esqueleto). */
  refreshing: boolean;
  reload: () => void;
}

export function usePolling<T>(path: string | null, initial: T | null = null, intervalMs?: number): PollState<T> {
  const configured = usePollInterval();
  const every = intervalMs ?? configured;
  const [data, setData] = useState<T | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const tick = useRef<() => void>(() => {});

  useEffect(() => {
    if (!path) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();

    const run = async () => {
      setRefreshing(true);
      try {
        const res = await fetch(path, { cache: "no-store", signal: controller.signal });
        if (!res.ok) throw new Error(`${res.status}`);
        const json = (await res.json()) as T;
        if (alive) {
          setData(json);
          setError(null);
        }
      } catch (e) {
        if (alive && !(e instanceof DOMException && e.name === "AbortError")) setError("Sin conexión con la API");
      } finally {
        if (alive) {
          setRefreshing(false);
          timer = setTimeout(run, every);
        }
      }
    };
    tick.current = () => {
      if (timer) clearTimeout(timer);
      void run();
    };
    timer = setTimeout(run, every);
    return () => {
      alive = false;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [path, every]);

  return { data, error, refreshing, reload: () => tick.current() };
}

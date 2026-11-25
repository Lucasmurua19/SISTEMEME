"use client";

import { useEffect, useState } from "react";

type Theme = "auto" | "light" | "dark";

function apply(theme: Theme) {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("auto");

  useEffect(() => {
    try {
      const saved = localStorage.getItem("theme");
      if (saved === "light" || saved === "dark") setTheme(saved);
    } catch {
      /* almacenamiento no disponible: se respeta el tema del sistema */
    }
  }, []);

  const next = () => {
    const order: Theme[] = ["auto", "light", "dark"];
    const n = order[(order.indexOf(theme) + 1) % order.length];
    setTheme(n);
    apply(n);
    try {
      if (n === "auto") localStorage.removeItem("theme");
      else localStorage.setItem("theme", n);
    } catch {
      /* ignorado */
    }
  };

  const label = theme === "auto" ? "Tema: sistema" : theme === "light" ? "Tema: claro" : "Tema: oscuro";
  return (
    <button type="button" className="btn btn-sm" onClick={next} aria-label={`${label}. Cambiar tema`} title={label}>
      {theme === "dark" ? "Oscuro" : theme === "light" ? "Claro" : "Auto"}
    </button>
  );
}

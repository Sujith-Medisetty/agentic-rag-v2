// Theme system: dark / light / system. Stored in localStorage so the user's
// pick survives reloads. Applied to <html data-theme="..."> which the CSS
// variables in index.css key off. `system` means "follow prefers-color-scheme"
// — done by NOT setting the attribute, letting the @media block win.

import { useEffect, useState, useCallback } from "react";

export type Theme = "light" | "dark" | "system";
const KEY = "agentic-rag.theme";

function readStored(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch { /* SSR / private mode */ }
  return "system";
}

function effectiveTheme(t: Theme): "light" | "dark" {
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function apply(theme: Theme): void {
  const html = document.documentElement;
  if (theme === "system") {
    html.removeAttribute("data-theme");
  } else {
    html.setAttribute("data-theme", theme);
  }
  // Keep the browser chrome (Android address bar) in sync.
  const meta = document.querySelector('meta[name="theme-color"]:not([media])') as HTMLMetaElement | null;
  if (meta) meta.content = effectiveTheme(theme) === "dark" ? "#1B1814" : "#FBF9F4";
}

/**
 * Call ONCE before React mounts (in main.tsx) so the first paint already has
 * the right colors — no FOUC flash from default light to dark.
 */
export function initThemeBeforeRender(): void {
  apply(readStored());
}

export function useTheme(): {
  theme: Theme;
  effective: "light" | "dark";
  setTheme: (t: Theme) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = useState<Theme>(readStored);
  const [effective, setEffective] = useState<"light" | "dark">(() =>
    effectiveTheme(readStored()),
  );

  useEffect(() => {
    apply(theme);
    setEffective(effectiveTheme(theme));
    try { localStorage.setItem(KEY, theme); } catch { /* ignore */ }
  }, [theme]);

  // Follow system changes when the user hasn't picked an explicit theme.
  useEffect(() => {
    if (theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      apply("system");
      setEffective(mq.matches ? "dark" : "light");
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(() => {
    setThemeState((curr) => {
      const eff = effectiveTheme(curr);
      return eff === "dark" ? "light" : "dark";
    });
  }, []);

  return { theme, effective, setTheme, toggle };
}

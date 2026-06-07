import { useEffect } from "react";
import Dashboard from "@/components/dashboard";

/**
 * The agent's real app replaces this file. The dashboard here is a
 * working example wired to the FastAPI backend: it GETs/POSTs/DELETEs
 * /api/items. Replace it with your real UI.
 */
export default function App() {
  // Register the service worker so the browser will surface the
  // install prompt. Skip in dev (Vite serves without a service worker).
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!import.meta.env.PROD) return;
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker
      .register("./sw.js")
      .catch((err) => console.warn("SW registration failed:", err));
  }, []);

  return <Dashboard />;
}

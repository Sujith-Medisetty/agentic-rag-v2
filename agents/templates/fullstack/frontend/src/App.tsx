import { useEffect } from "react";
import Dashboard from "@/components/dashboard";
// The fullstack template also has SectionsExample and ProductExample
// from the static template's component set. The Dashboard here is the
// default because fullstack apps usually have backend data to show.
// Swap to <SectionsExample /> or <ProductExample /> if the user's
// request is for a single-page product/portfolio site backed by the
// FastAPI server (e.g. for a contact form, signup, or webhook).

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

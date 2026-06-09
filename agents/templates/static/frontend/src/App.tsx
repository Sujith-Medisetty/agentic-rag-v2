import { useEffect } from "react";
import SectionsExample from "@/components/sections-example";
// import ProductExample from "@/components/product-example";

/**
 * The agent's real app replaces this file. The template ships TWO
 * example IAs the agent can copy from (or use as-is if they fit):
 *
 *   SectionsExample — long-form text sections (Hero / Overview /
 *                    Highlights / Connect). Best for portfolios,
 *                    personal sites, docs fronts, "about me" pages,
 *                    marketing landing pages.
 *
 *   ProductExample  — pricing tiers + feature comparison + FAQ.
 *                    Best for SaaS landings, product launches, B2B
 *                    pitch pages.
 *
 *   (both live in src/components/. Uncomment the import + swap the
 *   return to try the other one.)
 *
 * If the user's request is for a multi-page app (a dashboard, a
 * tool, a game, a productivity app), REPLACE these examples with
 * the right IA for the request — the template can't anticipate
 * every shape.
 */
export default function App() {
  // Register the service worker so the browser will surface the
  // install prompt. Skip in dev (Vite serves over :5180 without a
  // service worker).
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!import.meta.env.PROD) return;
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker
      .register("./sw.js")
      .catch((err) => console.warn("SW registration failed:", err));
  }, []);

  return <SectionsExample />;
}

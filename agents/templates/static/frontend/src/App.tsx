import { useEffect } from "react";
import SectionsExample from "@/components/sections-example";

/**
 * The agent's real app replaces this file. `SectionsExample` here
 * is a working example of the SINGLE-PAGE-SECTIONS layout the
 * agent will build 80% of the time: portfolio sites, landing
 * pages, docs front pages, marketing pages, "about me" pages.
 * The same shadcn primitives, theme, PWA bits, and animations --
 * just in the IA the user actually wants.
 *
 * If the user's request is for an app with multiple pages (a
 * dashboard, a tool, a game, a productivity app), REPLACE this
 * SectionsExample with the right IA for the request -- the
 * template can't anticipate every shape.
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

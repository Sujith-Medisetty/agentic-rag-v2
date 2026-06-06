import React from "react";
import ReactDOM from "react-dom/client";
import App from "@/App";
import { registerSW } from "@/pwa/registerSW";
import { initThemeBeforeRender } from "@/lib/theme";
import "@/lib/installPrompt";   // module-load side-effect: register the
                                 // `beforeinstallprompt` + `appinstalled`
                                 // window listeners BEFORE React mounts.
                                 // Otherwise we miss the event on returning
                                 // visits where Chrome fires it immediately.
import "@/index.css";

// Apply the stored theme BEFORE React renders so the first paint is in the
// right palette (no light → dark flash).
initThemeBeforeRender();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Register the service worker after the first paint so it doesn't compete
// for the main thread during startup.
registerSW();

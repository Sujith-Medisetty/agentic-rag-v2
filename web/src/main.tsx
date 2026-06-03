import React from "react";
import ReactDOM from "react-dom/client";
import App from "@/App";
import { registerSW } from "@/pwa/registerSW";
import "@/index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Register the service worker after the first paint so it doesn't compete
// for the main thread during startup.
registerSW();

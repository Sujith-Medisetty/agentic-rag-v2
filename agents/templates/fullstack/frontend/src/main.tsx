import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { ThemeProvider } from "@/components/theme-provider";
import { Toaster } from "@/components/ui/sonner";
import "@/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      <App />
      {/* Sonner <Toaster /> is the SINGLE place toasts are rendered.
          The template mounts it here at the app root, OUTSIDE the
          feature tree, so any component can call `toast(...)` and the
          toast appears exactly once. Do NOT also render <Toaster />
          in App.tsx or inside any feature component — that would
          duplicate the toaster and every toast would fire twice.
          Customise the position / richColors props here, not in App.tsx. */}
      <Toaster richColors position="top-center" />
    </ThemeProvider>
  </StrictMode>,
);

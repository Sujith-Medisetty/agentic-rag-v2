// Global app-settings store — instance-wide display toggles fetched once
// after auth and shared across the tree.
//
// These are NOT per-user preferences: they're box-wide toggles that only
// root can change (via the Admin page → adminApi.updateSettings), but every
// user's UI reads them. The canonical example is `tokensShowNewOnly`, which
// flips the per-LLM-call markers and the per-turn footer to show only the
// "new" (uncached) input tokens as the `in` figure.
//
// The provider lives INSIDE <AuthGate> (see App.tsx), so by the time it
// mounts the user is authenticated and GET /api/settings is safe to call.
// While the fetch is in flight we render with the safe default (everything
// off = the normal, full view), so the UI never blocks on this.

import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from "react";
import { settingsApi, type AppSettings } from "@/lib/api";

// Safe defaults — used before the first fetch resolves and if it fails.
// "off" means the normal full view (cached · new split everywhere), so a
// failed fetch degrades to the pre-existing behaviour rather than hiding data.
const DEFAULT_SETTINGS: AppSettings = {
  tokens_show_new_only: false,
};

interface AppSettingsValue {
  // The raw server settings (snake_case, mirrors the API payload).
  settings: AppSettings;
  // Convenience accessor for the one toggle the token UI cares about.
  tokensShowNewOnly: boolean;
  // Replace the cached settings locally (Admin calls this with the PATCH
  // response so the change is reflected instantly without a refetch).
  apply: (next: AppSettings) => void;
  // Re-fetch from the server (e.g. if another admin changed it elsewhere).
  refresh: () => Promise<void>;
}

const AppSettingsContext = createContext<AppSettingsValue | null>(null);

export function AppSettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<AppSettings>(DEFAULT_SETTINGS);

  const refresh = useCallback(async () => {
    try {
      const s = await settingsApi.get();
      setSettings(s);
    } catch {
      // Keep whatever we have (defaults on first load). A transient failure
      // shouldn't blow away a previously-fetched value or block rendering.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<AppSettingsValue>(() => ({
    settings,
    tokensShowNewOnly: settings.tokens_show_new_only,
    apply: setSettings,
    refresh,
  }), [settings, refresh]);

  return (
    <AppSettingsContext.Provider value={value}>
      {children}
    </AppSettingsContext.Provider>
  );
}

export function useAppSettings(): AppSettingsValue {
  const v = useContext(AppSettingsContext);
  if (!v) {
    throw new Error("useAppSettings must be called inside <AppSettingsProvider>");
  }
  return v;
}

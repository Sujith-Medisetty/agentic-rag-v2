import { BrowserRouter, Routes, Route } from "react-router-dom";
import AuthGate from "@/pages/AuthGate";
import Layout from "@/components/Layout";
import Workspace from "@/pages/Workspace";
import ChatPage from "@/pages/ChatPage";
import ProjectList from "@/pages/ProjectList";
import SessionList from "@/pages/SessionList";
import Admin from "@/pages/Admin";
import Settings from "@/pages/Settings";
import { SessionProvider } from "@/lib/sessionContext";
import { AppSettingsProvider } from "@/lib/appSettings";
import { GlobalToast } from "@/components/GlobalToast";

// No custom PWA install UI. Users install via the browser's native
// flow (Chrome/Edge URL-bar install icon, iOS Share → Add to Home
// Screen). The manifest + service worker are still served so the
// browser CAN offer install — we just don't render our own banner
// or button on top of it. Cleaner code, no React-state-vs-event
// capture issues, no over-engineered diagnostics.
//
// The only "PWA UI" left is the service-worker-update toast (in
// Layout), which is a different concern: telling the user when a
// new build is available, not prompting install.

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <AppSettingsProvider>
          <SessionProvider>
          <Routes>
            {/* Default landing: sidebar workspace, optionally with a session
                active via /s/:sessionId. Replaces the old project-list →
                session-list → chat funnel. */}
            <Route path="/" element={<Workspace />}>
              <Route path="s/:sessionId" element={<ChatPage />} />
            </Route>

            {/* Legacy project-list / session-list pages still reachable for
                users with multiple projects. The new sidebar can be expanded
                to surface these later. */}
            {/* Root-only admin panel. The component itself enforces the role
                check + redirects non-root users to /, so an accidental link
                tap doesn't leak the page. */}
            <Route path="/admin" element={<Layout><Admin /></Layout>} />

            {/* Settings: pause/resume toggles for all the user's deployed
                apps, grouped by source session. Root sees every app;
                non-root sees their own + orphans. */}
            <Route
              path="/settings"
              element={<Layout><Settings /></Layout>}
            />

            <Route
              path="/projects"
              element={<Layout><ProjectList /></Layout>}
            />
            <Route
              path="/p/:projectId"
              element={<Layout><SessionList /></Layout>}
            />
            {/* Legacy chat route still works for deep links into specific
                projects. Renders ChatPage WITHOUT the Workspace sidebar so
                bookmarks from the old shape don't break. */}
            <Route
              path="/p/:projectId/s/:sessionId"
              element={<ChatPage />}
            />

            <Route
              path="*"
              element={
                <Layout>
                  <div className="mx-auto max-w-3xl p-6 text-muted">
                    Not found.
                  </div>
                </Layout>
              }
            />
          </Routes>

          {/* The single toast renderer — anywhere in the tree can call
              useSessions().setToast() to show a message. The toast
              itself is rendered ONCE at the App level so it survives
              route changes. */}
          <GlobalToast />
          </SessionProvider>
        </AppSettingsProvider>
      </AuthGate>
    </BrowserRouter>
  );
}

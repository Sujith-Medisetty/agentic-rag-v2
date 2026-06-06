import { BrowserRouter, Routes, Route } from "react-router-dom";
import AuthGate from "@/pages/AuthGate";
import Layout from "@/components/Layout";
import Workspace from "@/pages/Workspace";
import ChatPage from "@/pages/ChatPage";
import ProjectList from "@/pages/ProjectList";
import SessionList from "@/pages/SessionList";
import Admin from "@/pages/Admin";
import InstallPrompt from "@/components/InstallPrompt";
import IosInstallHint from "@/components/IosInstallHint";
import { SessionProvider } from "@/lib/sessionContext";
import { GlobalToast } from "@/components/GlobalToast";

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate>
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
          <InstallPrompt />
          <IosInstallHint />
        </SessionProvider>
      </AuthGate>
    </BrowserRouter>
  );
}

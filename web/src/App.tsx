import { BrowserRouter, Routes, Route } from "react-router-dom";
import AuthGate from "@/pages/AuthGate";
import Layout from "@/components/Layout";
import ProjectList from "@/pages/ProjectList";
import SessionList from "@/pages/SessionList";
import ChatPage from "@/pages/ChatPage";
import InstallPrompt from "@/components/InstallPrompt";
import IosInstallHint from "@/components/IosInstallHint";

export default function App() {
  return (
    <BrowserRouter>
      <AuthGate>
        <Routes>
          {/* Chat takes the full viewport — no Layout chrome */}
          <Route path="/p/:projectId/s/:sessionId" element={<ChatPage />} />

          {/* Project + session list pages share the top-bar Layout */}
          <Route path="/" element={<Layout><ProjectList /></Layout>} />
          <Route
            path="/p/:projectId"
            element={<Layout><SessionList /></Layout>}
          />

          {/* Fallback */}
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

        {/* Floating install prompts. Each component decides whether to render
            itself based on browser + standalone state, so it's safe to mount
            them globally. */}
        <InstallPrompt />
        <IosInstallHint />
      </AuthGate>
    </BrowserRouter>
  );
}

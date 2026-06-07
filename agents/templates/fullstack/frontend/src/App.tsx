import { useEffect, useState } from "react";

interface Item {
  id: number;
  title: string;
  done: boolean;
}

// `base` from vite.config.ts (set to "./") is exposed at build time
// via import.meta.env.BASE_URL. We append "/api" because Caddy proxies
// /api/* to the per-app backend port.
const API = (import.meta as any).env.BASE_URL.replace(/\/$/, "") + "/api";

/**
 * Ojas fullstack app — example with a list + add + toggle.
 *
 * `API` is the path prefix for the backend. In dev (vite on :5180),
 * the backend runs on :8000 and the dev server proxies /api/* to it.
 * In production (built dist served by Caddy), Caddy already proxies
 * /api/* to the per-app backend port, so this just works.
 *
 * Replace this with your real app.
 */
export default function App() {
  const [items, setItems] = useState<Item[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/items`)
      .then((r) => r.json())
      .then(setItems)
      .catch((e) => setError(String(e)));
  }, []);

  const add = async () => {
    if (!draft.trim()) return;
    const r = await fetch(`${API}/items`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: draft, done: false }),
    });
    const created = await r.json();
    setItems((prev) => [...prev, created]);
    setDraft("");
  };

  const toggle = async (id: number) => {
    const item = items.find((i) => i.id === id);
    if (!item) return;
    const r = await fetch(`${API}/items/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ title: item.title, done: !item.done }),
    });
    const updated = await r.json();
    setItems((prev) => prev.map((i) => (i.id === id ? updated : i)));
  };

  return (
    <main style={{ maxWidth: 480, margin: "4rem auto", font: "16px system-ui, sans-serif" }}>
      <h1 style={{ marginBottom: "1rem" }}>Fullstack app</h1>
      <p style={{ color: "#666", marginBottom: "1.5rem" }}>
        React + FastAPI + SQLite, deployed by Ojas.
      </p>
      {error && <p style={{ color: "#c00" }}>Error: {error}</p>}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
        style={{ display: "flex", gap: 8, marginBottom: "1rem" }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Add an item…"
          style={{ flex: 1, padding: "0.5rem", font: "inherit" }}
        />
        <button type="submit" style={{ padding: "0.5rem 1rem" }}>Add</button>
      </form>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {items.map((i) => (
          <li
            key={i.id}
            onClick={() => toggle(i.id)}
            style={{
              padding: "0.5rem 0.75rem",
              borderBottom: "1px solid #eee",
              cursor: "pointer",
              textDecoration: i.done ? "line-through" : "none",
              color: i.done ? "#888" : "inherit",
            }}
          >
            {i.title}
          </li>
        ))}
      </ul>
      {items.length === 0 && (
        <p style={{ color: "#888", fontStyle: "italic" }}>No items yet.</p>
      )}
    </main>
  );
}

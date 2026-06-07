// Ojas STATIC app — example with a todo list, localStorage persistence,
// and the InstallButton. No backend, no API, no port — everything
// lives in the browser.
//
// Replace this with your real app. Keep the InstallButton rendered
// somewhere persistently visible until the user installs the PWA.

import { useEffect, useState } from "react";
import InstallButton from "./components/InstallButton";

interface Item {
  id: number;
  title: string;
  done: boolean;
}

const STORAGE_KEY = "ojas.items.v1";

function loadItems(): Item[] {
  if (typeof localStorage === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Item[]) : [];
  } catch {
    return [];
  }
}

function saveItems(items: Item[]) {
  if (typeof localStorage === "undefined") return;
  localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
}

export default function App() {
  const [items, setItems] = useState<Item[]>(loadItems);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    saveItems(items);
  }, [items]);

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

  const add = () => {
    const title = draft.trim();
    if (!title) return;
    setItems((prev) => [
      ...prev,
      { id: Date.now(), title, done: false },
    ]);
    setDraft("");
  };

  const toggle = (id: number) => {
    setItems((prev) =>
      prev.map((i) => (i.id === id ? { ...i, done: !i.done } : i))
    );
  };

  const remove = (id: number) => {
    setItems((prev) => prev.filter((i) => i.id !== id));
  };

  const remaining = items.filter((i) => !i.done).length;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900 font-sans antialiased">
      <div className="mx-auto max-w-xl px-4 py-8">
        <header className="mb-6 flex items-center gap-3">
          <h1 className="font-serif text-2xl font-semibold tracking-tight">
            Todos
          </h1>
          <span className="ml-auto" />
          <InstallButton />
        </header>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            add();
          }}
          className="flex gap-2"
        >
          <input
            type="text"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="What needs doing?"
            aria-label="New todo"
            className="flex-1 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500"
          />
          <button
            type="submit"
            className="rounded-md bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Add
          </button>
        </form>

        <p className="mt-3 text-xs text-slate-500">
          {items.length === 0
            ? "Nothing here yet."
            : `${remaining} of ${items.length} remaining.`}
        </p>

        <ul className="mt-4 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
          {items.map((it) => (
            <li
              key={it.id}
              className="flex items-center gap-3 px-3 py-2 text-sm"
            >
              <input
                type="checkbox"
                checked={it.done}
                onChange={() => toggle(it.id)}
                aria-label={`Toggle "${it.title}"`}
                className="size-4 accent-indigo-600"
              />
              <span
                className={`flex-1 ${
                  it.done ? "line-through text-slate-400" : "text-slate-800"
                }`}
              >
                {it.title}
              </span>
              <button
                type="button"
                onClick={() => remove(it.id)}
                aria-label={`Remove "${it.title}"`}
                className="text-slate-400 hover:text-rose-600"
              >
                ×
              </button>
            </li>
          ))}
        </ul>

        <footer className="mt-8 text-center text-xs text-slate-400">
          Stored in your browser only. No server, no account.
        </footer>
      </div>
    </div>
  );
}

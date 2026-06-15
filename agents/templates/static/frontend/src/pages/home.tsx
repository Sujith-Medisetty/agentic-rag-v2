import { Link } from "react-router-dom";

/**
 * Default landing route. The starter sections layout is mounted
 * at "/" by App.tsx; this page is here as a copy-paste reference
 * for adding NEW routes.
 *
 * To add another page:
 *   1. Create `src/pages/<name>.tsx` that exports a default
 *      React component (a real one, not this stub).
 *   2. Register it in `App.tsx` inside `<Routes>`:
 *        <Route path="/<name>" element={<NamePage />} />
 *   3. Link to it with `<Link to="/<name>">…</Link>` — do NOT
 *      use `<a href>` for in-app navigation or the whole app
 *      will full-reload and reset scroll/state.
 *
 * Catch-all: `<Route path="*" element={<NotFoundPage />} />` in
 * App.tsx handles deep links and typo'd URLs.
 */
export default function HomePage() {
  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Home</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        This is the default route. Add new pages under{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-xs">
          src/pages/
        </code>{" "}
        and register them in <code className="rounded bg-muted px-1 py-0.5 text-xs">App.tsx</code>.
      </p>
      <p className="mt-4 text-sm">
        Example link (won't 404 thanks to the catch-all):{" "}
        <Link to="/missing" className="text-primary underline">
          /missing
        </Link>
      </p>
    </div>
  );
}

import { Link } from "react-router-dom";

/**
 * 404 / catch-all route. Catches every URL that doesn't match a
 * declared route — important for client-side routing because
 * Caddy `try_files {path} /index.html` will serve this app for
 * ANY deep URL, including the ones that don't have a page.
 */
export default function NotFoundPage() {
  return (
    <div className="mx-auto max-w-md px-4 py-16 text-center">
      <h1 className="text-3xl font-semibold tracking-tight">Page not found</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The page you&apos;re looking for doesn&apos;t exist.
      </p>
      <Link
        to="/"
        className="mt-6 inline-block rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
      >
        Go home
      </Link>
    </div>
  );
}

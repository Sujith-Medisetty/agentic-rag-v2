/**
 * SectionsExample — a working single-page-sections layout the
 * agent can use as the starting point for portfolio / landing /
 * marketing / docs sites. Sticky top nav with anchor links,
 * a Hero, an About section, a Features grid, a Contact strip,
 * a footer. Same shadcn primitives + theme + PWA bits as the
 * rest of the template; just structured the way the user
 * usually wants the IA to look for these use cases.
 *
 * Replace the section bodies with the real content for the
 * request; keep the same nav + theme + PWA wiring.
 */
import { ArrowUpRight, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ThemeToggle } from "@/components/theme-toggle";
import InstallButton from "@/components/install-button";

const NAV = [
  { id: "about", label: "About" },
  { id: "features", label: "Features" },
  { id: "contact", label: "Contact" },
] as const;

const FEATURES = [
  {
    title: "Ship faster",
    body: "Skip the boilerplate. The Ojas template gives you a working app, a real build pipeline, and a public URL on first deploy.",
  },
  {
    title: "Real TypeScript",
    body: "Strict types, tsx, path aliases, and a render smoke test that catches duplicate-React and broken imports before you ship.",
  },
  {
    title: "PWA-ready",
    body: "Installable from the browser, works offline, and ships with manifest + service worker wired up out of the box.",
  },
] as const;

export default function SectionsExample() {
  const scrollTo = (id: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    const el = document.getElementById(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Sticky top nav — desktop */}
      <header className="sticky top-0 z-30 hidden border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:block">
        <nav className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-6">
          <a
            href="#top"
            onClick={scrollTo("top")}
            className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight"
          >
            <Sparkles className="h-4 w-4 text-accent" />
            Ojas
          </a>
          <div className="flex gap-1">
            {NAV.map(({ id, label }) => (
              <a
                key={id}
                href={`#${id}`}
                onClick={scrollTo(id)}
                className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/10 hover:text-foreground"
              >
                {label}
              </a>
            ))}
          </div>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <InstallButton />
          </div>
        </nav>
      </header>

      {/* Mobile top bar — just theme + install + brand */}
      <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border/60 bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:hidden">
        <span className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight">
          <Sparkles className="h-4 w-4 text-accent" />
          Ojas
        </span>
        <div className="flex items-center gap-1">
          <ThemeToggle />
          <InstallButton />
        </div>
      </header>

      <main id="top" className="mx-auto max-w-5xl px-4 sm:px-6">
        {/* Hero */}
        <section className="pt-16 pb-20 sm:pt-24 sm:pb-28">
          <div className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-elevated/40 px-3 py-1 text-xs text-muted-foreground">
            <span className="size-1.5 rounded-full bg-success" />
            Deployed by Ojas
          </div>
          <h1 className="mt-4 font-serif text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Build a real app, ship a real URL.
          </h1>
          <p className="mt-4 max-w-2xl text-base text-muted-foreground sm:text-lg">
            Replace this with the real headline for your product,
            portfolio, or landing page. The same nav, theme, and
            PWA wiring works for any single-page-sections layout.
          </p>
          <div className="mt-6 flex flex-wrap items-center gap-3">
            <Button asChild>
              <a href="#contact" onClick={scrollTo("contact")}>
                Get in touch
                <ArrowUpRight className="ml-1 h-4 w-4" />
              </a>
            </Button>
            <Button variant="outline" asChild>
              <a href="#features" onClick={scrollTo("features")}>
                See features
              </a>
            </Button>
          </div>
        </section>

        {/* About */}
        <section id="about" className="border-t border-border/60 py-16 sm:py-20">
          <div className="grid gap-8 sm:grid-cols-3">
            <div>
              <div className="text-xs font-medium uppercase tracking-[0.18em] text-subtle">
                About
              </div>
              <h2 className="mt-2 font-serif text-2xl font-semibold tracking-tight sm:text-3xl">
                A bit about this
              </h2>
            </div>
            <p className="sm:col-span-2 text-base text-muted-foreground">
              Replace this with a 2-3 sentence pitch: who you are,
              what this is for, and why the visitor should care.
              One tight paragraph beats a wall of text every time.
            </p>
          </div>
        </section>

        {/* Features */}
        <section
          id="features"
          className="border-t border-border/60 py-16 sm:py-20"
        >
          <div className="text-xs font-medium uppercase tracking-[0.18em] text-subtle">
            Features
          </div>
          <h2 className="mt-2 font-serif text-2xl font-semibold tracking-tight sm:text-3xl">
            What you get out of the box
          </h2>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((f) => (
              <Card key={f.title}>
                <CardContent className="space-y-2 pt-6">
                  <div className="text-base font-semibold tracking-tight">
                    {f.title}
                  </div>
                  <p className="text-sm text-muted-foreground">{f.body}</p>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>

        {/* Contact */}
        <section
          id="contact"
          className="border-t border-border/60 py-16 sm:py-20"
        >
          <div className="text-xs font-medium uppercase tracking-[0.18em] text-subtle">
            Contact
          </div>
          <h2 className="mt-2 font-serif text-2xl font-semibold tracking-tight sm:text-3xl">
            Get in touch
          </h2>
          <p className="mt-3 max-w-2xl text-base text-muted-foreground">
            Drop your real contact details here -- email, social
            links, a contact form, or a Calendly URL. The Ojas
            static template can call any public form endpoint
            from the browser; if you need a server-side form,
            escalate to the fullstack scaffold.
          </p>
          <div className="mt-6 flex flex-wrap gap-3">
            <Button asChild>
              <a href="mailto:hello@example.com">Email me</a>
            </Button>
          </div>
        </section>
      </main>

      <footer className="border-t border-border/60">
        <div className="mx-auto flex max-w-5xl flex-col gap-2 px-4 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <span>© {new Date().getFullYear()} Your name. Built with Ojas.</span>
          <span className="text-xs">
            Replace this footer with your own links.
          </span>
        </div>
      </footer>
    </div>
  );
}

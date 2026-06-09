/**
 * SectionsExample — a working single-page-sections layout the agent
 * can use as the starting point for portfolio / landing / marketing /
 * docs sites. Sticky top nav with anchor links, a Hero, an Overview
 * section, a Highlights grid, a Connect strip, a footer.
 *
 * Replace the section bodies with the real content for the request;
 * keep the same nav + theme + PWA wiring.
 *
 * Note on nav labels — the items below ("Overview / Highlights /
 * Connect") are deliberately GENERIC so this example reads as either
 * a portfolio, a product page, a startup landing, a docs front, or
 * an "about me" page. The agent should rename them to match whatever
 * the user's request actually is.
 */
import { ArrowUpRight, Sparkles } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ThemeToggle } from "@/components/theme-toggle";
import InstallButton from "@/components/install-button";
import { cardIn, fadeUp, hover, stagger, tap } from "@/lib/motion";

// Each entry: { id, label }. The agent should rename labels to match
// the section's actual content (e.g. for a docs front: "Guide / API / Changelog").
const NAV = [
  { id: "overview",  label: "Overview" },
  { id: "highlights", label: "Highlights" },
  { id: "connect",   label: "Connect" },
] as const;

const HIGHLIGHTS = [
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

function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

/**
 * NavLinks — renders the desktop + mobile nav anchor list. Extracted
 * from the body so the desktop and mobile headers stay in sync and
 * the agent can add a section by editing NAV + nothing else.
 */
function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <>
      {NAV.map(({ id, label }) => (
        <a
          key={id}
          href={`#${id}`}
          onClick={(e) => {
            e.preventDefault();
            scrollToId(id);
            onNavigate?.();
          }}
          className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
        >
          {label}
        </a>
      ))}
    </>
  );
}

export default function SectionsExample() {
  const reduce = useReducedMotion();

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* Sticky top nav — desktop */}
      <motion.header
        initial={reduce ? false : { y: -20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
        className="sticky top-0 z-30 hidden border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:block"
      >
        <nav className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-6">
          <a
            href="#top"
            onClick={(e) => {
              e.preventDefault();
              scrollToId("top");
            }}
            className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight"
          >
            <Sparkles className="h-4 w-4 text-accent" />
            Ojas
          </a>
          <div className="flex gap-1">
            <NavLinks />
          </div>
          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <InstallButton />
          </div>
        </nav>
      </motion.header>

      {/* Mobile top bar */}
      <motion.header
        initial={reduce ? false : { y: -20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
        className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border/60 bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:hidden"
      >
        <span className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight">
          <Sparkles className="h-4 w-4 text-accent" />
          Ojas
        </span>
        <div className="flex items-center gap-1">
          <ThemeToggle />
          <InstallButton />
        </div>
      </motion.header>

      <main id="top" className="mx-auto max-w-5xl px-4 sm:px-6">
        {/* Hero */}
        <section className="pt-16 pb-20 sm:pt-24 sm:pb-28">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-muted/40 px-3 py-1 text-xs text-muted-foreground"
          >
            <span className="size-1.5 rounded-full bg-success" />
            Deployed by Ojas
          </motion.div>
          <motion.h1
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            transition={{ delay: 0.05 }}
            className="mt-4 text-4xl font-semibold leading-tight tracking-tight sm:text-5xl"
          >
            Build a real app, ship a real URL.
          </motion.h1>
          <motion.p
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            transition={{ delay: 0.1 }}
            className="mt-4 max-w-2xl text-base text-muted-foreground sm:text-lg"
          >
            Replace this with the real headline for your product,
            portfolio, or landing page. The same nav, theme, and
            PWA wiring works for any single-page-sections layout.
          </motion.p>
          <motion.div
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            transition={{ delay: 0.15 }}
            className="mt-6 flex flex-wrap items-center gap-3"
          >
            <motion.div whileHover={hover} whileTap={tap}>
              <Button asChild>
                <a
                  href="#connect"
                  onClick={(e) => {
                    e.preventDefault();
                    scrollToId("connect");
                  }}
                >
                  Get in touch
                  <ArrowUpRight className="ml-1 h-4 w-4" />
                </a>
              </Button>
            </motion.div>
            <motion.div whileHover={hover} whileTap={tap}>
              <Button variant="outline" asChild>
                <a
                  href="#highlights"
                  onClick={(e) => {
                    e.preventDefault();
                    scrollToId("highlights");
                  }}
                >
                  See highlights
                </a>
              </Button>
            </motion.div>
          </motion.div>
        </section>

        {/* Overview */}
        <section id="overview" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
            className="grid gap-8 sm:grid-cols-3"
          >
            <div>
              <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
                Overview
              </div>
              <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
                What this is
              </h2>
            </div>
            <p className="sm:col-span-2 text-base text-muted-foreground">
              Replace this with a 2-3 sentence pitch: what this is,
              who it's for, and why the visitor should care. One tight
              paragraph beats a wall of text every time.
            </p>
          </motion.div>
        </section>

        {/* Highlights */}
        <section id="highlights" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
          >
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
              Highlights
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              What you get out of the box
            </h2>
          </motion.div>
          <motion.div
            variants={stagger}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-10%" }}
            className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3"
          >
            {HIGHLIGHTS.map((h) => (
              <motion.div key={h.title} variants={cardIn}>
                <Card className="h-full transition-colors hover:bg-muted/30">
                  <CardContent className="space-y-2 pt-6">
                    <div className="text-base font-semibold tracking-tight">
                      {h.title}
                    </div>
                    <p className="text-sm text-muted-foreground">{h.body}</p>
                  </CardContent>
                </Card>
              </motion.div>
            ))}
          </motion.div>
        </section>

        {/* Connect */}
        <section id="connect" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
          >
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
              Connect
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              Get in touch
            </h2>
            <p className="mt-3 max-w-2xl text-base text-muted-foreground">
              Drop your real contact details here — email, social
              links, a contact form, or a Calendly URL.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <motion.div whileHover={hover} whileTap={tap}>
                <Button asChild>
                  <a href="mailto:hello@example.com">Email me</a>
                </Button>
              </motion.div>
            </div>
          </motion.div>
        </section>
      </main>

      <motion.footer
        variants={fadeUp}
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true }}
        className="border-t border-border/60"
      >
        <div className="mx-auto flex max-w-5xl flex-col gap-2 px-4 py-8 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <span>© {new Date().getFullYear()} Your name. Built with Ojas.</span>
          <span className="text-xs">Replace this footer with your own links.</span>
        </div>
      </motion.footer>
    </div>
  );
}

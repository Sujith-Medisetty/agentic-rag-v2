/**
 * ProductExample — a working single-page product layout: hero,
 * feature comparison, pricing tiers, FAQ, footer. Different IA from
 * SectionsExample so the agent has TWO patterns to copy from:
 *
 *   SectionsExample → portfolio / docs front / personal site
 *                    (long-form copy, single column of text sections)
 *   ProductExample  → SaaS landing / pricing page / feature pitch
 *                    (comparison tables, pricing cards, FAQ)
 *
 * Pick whichever matches the user's request, or replace both with the
 * real IA. The same nav, theme, and PWA wiring is shared.
 */
import { Check, Sparkles, X } from "lucide-react";
import { motion, useReducedMotion } from "framer-motion";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ThemeToggle } from "@/components/theme-toggle";
import InstallButton from "@/components/install-button";
import { cardIn, fadeUp, hover, stagger, tap } from "@/lib/motion";

const NAV = [
  { id: "features", label: "Features" },
  { id: "pricing",  label: "Pricing" },
  { id: "faq",      label: "FAQ" },
] as const;

const PLANS = [
  {
    name: "Free",
    price: "$0",
    cadence: "forever",
    description: "Try it out, kick the tires.",
    cta: "Get started",
    features: ["Up to 3 projects", "Community support", "Single user"],
    highlighted: false,
  },
  {
    name: "Pro",
    price: "$12",
    cadence: "per user / month",
    description: "For small teams shipping real things.",
    cta: "Start a 14-day trial",
    features: [
      "Unlimited projects",
      "Priority email support",
      "Up to 10 team members",
      "Custom domains",
    ],
    highlighted: true,
  },
  {
    name: "Team",
    price: "$48",
    cadence: "per month, 5 users",
    description: "Bigger teams + audit + SSO.",
    cta: "Talk to sales",
    features: [
      "Everything in Pro",
      "Unlimited team members",
      "SSO + SCIM",
      "Audit log + retention",
    ],
    highlighted: false,
  },
] as const;

const COMPARISON = [
  { feature: "Projects",          free: "3",       pro: "Unlimited", team: "Unlimited" },
  { feature: "Team members",      free: "1",       pro: "10",        team: "Unlimited" },
  { feature: "Custom domains",    free: false,     pro: true,        team: true },
  { feature: "Priority support",  free: false,     pro: true,        team: true },
  { feature: "SSO / SCIM",        free: false,     pro: false,       team: true },
  { feature: "Audit log",         free: false,     pro: false,       team: true },
] as const;

const FAQ = [
  {
    q: "Can I switch plans later?",
    a: "Yes — upgrade or downgrade at any time. Prorated for the current billing period.",
  },
  {
    q: "Do you offer a free trial of paid plans?",
    a: "Pro comes with a 14-day trial, no card required. Team plans include a 30-day pilot.",
  },
  {
    q: "How is pricing calculated for the Team plan?",
    a: "Flat $48/month includes 5 seats. Additional seats are $9/month each.",
  },
] as const;

function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function NavLinks() {
  return (
    <>
      {NAV.map(({ id, label }) => (
        <a
          key={id}
          href={`#${id}`}
          onClick={(e) => {
            e.preventDefault();
            scrollToId(id);
          }}
          className="rounded-md px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/40 hover:text-foreground"
        >
          {label}
        </a>
      ))}
    </>
  );
}

export default function ProductExample() {
  const reduce = useReducedMotion();

  return (
    <div className="min-h-screen bg-background text-foreground">
      <motion.header
        initial={reduce ? false : { y: -20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
        className="sticky top-0 z-30 hidden border-b border-border/60 bg-background/80 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:block"
      >
        <nav className="mx-auto flex h-14 max-w-5xl items-center gap-6 px-6">
          <a
            href="#top"
            onClick={(e) => { e.preventDefault(); scrollToId("top"); }}
            className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight"
          >
            <Sparkles className="h-4 w-4 text-accent" />
            Product
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

      <motion.header
        initial={reduce ? false : { y: -20, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
        className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border/60 bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 md:hidden"
      >
        <span className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight">
          <Sparkles className="h-4 w-4 text-accent" />
          Product
        </span>
        <div className="flex items-center gap-1">
          <ThemeToggle />
          <InstallButton />
        </div>
      </motion.header>

      <main id="top" className="mx-auto max-w-5xl px-4 sm:px-6">
        {/* Hero */}
        <section className="pt-16 pb-16 text-center sm:pt-24 sm:pb-20">
          <motion.h1
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            className="mx-auto max-w-3xl text-4xl font-semibold leading-tight tracking-tight sm:text-5xl"
          >
            The product headline goes here.
          </motion.h1>
          <motion.p
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            transition={{ delay: 0.08 }}
            className="mx-auto mt-4 max-w-2xl text-base text-muted-foreground sm:text-lg"
          >
            One tight sentence about the value prop. Replace with what
            your product actually does for the customer.
          </motion.p>
          <motion.div
            variants={fadeUp}
            initial="hidden"
            animate="visible"
            transition={{ delay: 0.16 }}
            className="mt-6 flex flex-wrap items-center justify-center gap-3"
          >
            <motion.div whileHover={hover} whileTap={tap}>
              <Button asChild>
                <a href="#pricing" onClick={(e) => { e.preventDefault(); scrollToId("pricing"); }}>
                  See pricing
                </a>
              </Button>
            </motion.div>
            <motion.div whileHover={hover} whileTap={tap}>
              <Button variant="outline" asChild>
                <a href="#features" onClick={(e) => { e.preventDefault(); scrollToId("features"); }}>
                  How it works
                </a>
              </Button>
            </motion.div>
          </motion.div>
        </section>

        {/* Features (comparison table) */}
        <section id="features" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
          >
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
              Features
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              What you get on each plan
            </h2>
          </motion.div>
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-10%" }}
            transition={{ delay: 0.05 }}
            className="mt-8 overflow-x-auto rounded-lg border border-border/60"
          >
            <table className="w-full text-left text-sm">
              <thead className="bg-muted/40 text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-medium">Feature</th>
                  <th className="px-4 py-3 font-medium">Free</th>
                  <th className="px-4 py-3 font-medium">Pro</th>
                  <th className="px-4 py-3 font-medium">Team</th>
                </tr>
              </thead>
              <tbody>
                {COMPARISON.map((row, i) => (
                  <tr
                    key={row.feature}
                    className={i > 0 ? "border-t border-border/60" : ""}
                  >
                    <td className="px-4 py-3 font-medium">{row.feature}</td>
                    {(["free", "pro", "team"] as const).map((col) => {
                      const v = row[col];
                      return (
                        <td key={col} className="px-4 py-3 text-muted-foreground">
                          {v === true ? (
                            <Check className="h-4 w-4 text-success" />
                          ) : v === false ? (
                            <X className="h-4 w-4 text-muted-foreground/40" />
                          ) : (
                            <span className="font-mono">{v}</span>
                          )}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </motion.div>
        </section>

        {/* Pricing */}
        <section id="pricing" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
          >
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
              Pricing
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              Simple plans, no surprises
            </h2>
          </motion.div>
          <motion.div
            variants={stagger}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-10%" }}
            className="mt-8 grid gap-4 sm:grid-cols-3"
          >
            {PLANS.map((plan) => (
              <motion.div key={plan.name} variants={cardIn}>
                <Card
                  className={
                    plan.highlighted
                      ? "h-full border-primary/50 shadow-md"
                      : "h-full"
                  }
                >
                  <CardHeader>
                    <CardTitle className="flex items-baseline justify-between">
                      <span>{plan.name}</span>
                      {plan.highlighted && (
                        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                          Popular
                        </span>
                      )}
                    </CardTitle>
                    <div className="mt-2 flex items-baseline gap-1">
                      <span className="font-mono text-3xl font-semibold">
                        {plan.price}
                      </span>
                      <span className="text-sm text-muted-foreground">
                        {plan.cadence}
                      </span>
                    </div>
                    <p className="text-sm text-muted-foreground">
                      {plan.description}
                    </p>
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <ul className="space-y-2 text-sm">
                      {plan.features.map((f) => (
                        <li key={f} className="flex items-start gap-2">
                          <Check className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                          <span>{f}</span>
                        </li>
                      ))}
                    </ul>
                    <motion.div whileHover={hover} whileTap={tap}>
                      <Button
                        className="w-full"
                        variant={plan.highlighted ? "default" : "outline"}
                      >
                        {plan.cta}
                      </Button>
                    </motion.div>
                  </CardContent>
                </Card>
              </motion.div>
            ))}
          </motion.div>
        </section>

        {/* FAQ */}
        <section id="faq" className="border-t border-border/60 py-16 sm:py-20">
          <motion.div
            variants={fadeUp}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-15%" }}
          >
            <div className="text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground/70">
              FAQ
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
              Common questions
            </h2>
          </motion.div>
          <motion.dl
            variants={stagger}
            initial="hidden"
            whileInView="visible"
            viewport={{ once: true, margin: "-10%" }}
            className="mt-8 space-y-6"
          >
            {FAQ.map(({ q, a }) => (
              <motion.div
                key={q}
                variants={fadeUp}
                className="border-b border-border/60 pb-6 last:border-0"
              >
                <dt className="text-base font-semibold tracking-tight">{q}</dt>
                <dd className="mt-2 text-sm text-muted-foreground">{a}</dd>
              </motion.div>
            ))}
          </motion.dl>
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
          <span>© {new Date().getFullYear()} Your company. Built with Ojas.</span>
          <span className="text-xs">Replace this footer with your own links.</span>
        </div>
      </motion.footer>
    </div>
  );
}

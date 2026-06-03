import type { Config } from "tailwindcss";

/**
 * Refined dark palette — warm neutrals, teal primary, soft violet secondary
 * (used only for accent gradients / brand mark, never for chrome).
 *
 *   bg        ▼ darkest layer, the page background
 *   surface     cards / panels sitting on top of bg
 *   elevated    inputs / hover states / nested cards inside surface
 *   border      hairline borders (subtle, not harsh)
 *   text        primary text (body)
 *   muted       secondary text (metadata, captions)
 *   subtle      tertiary text (timestamps, very low priority)
 *   accent      primary accent — teal
 *   accent-2    secondary accent — soft violet (gradients only)
 *   success     positive status (commit, tool done)
 *   warn        warnings (dirty branch, paused, stash)
 *   danger      errors (failed commit/push, system messages)
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Inter", "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont",
          "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif",
        ],
        mono: [
          "ui-monospace", "SFMono-Regular", "SF Mono", "Menlo",
          "Monaco", "Consolas", "Liberation Mono", "Courier New", "monospace",
        ],
      },
      colors: {
        bg:        "#07080a",   // page
        surface:   "#0f1115",   // cards on bg
        elevated:  "#171a20",   // inputs / nested cards
        border:    "#262a33",   // hairlines
        text:      "#ecedf0",   // primary
        muted:     "#9097a3",   // secondary
        subtle:    "#5e6470",   // tertiary
        accent:    "#5eead4",   // ONE accent (teal)
        "accent-2": "#a78bfa",  // soft violet — used in gradients only
        success:   "#34d399",
        warn:      "#fbbf24",
        danger:    "#f87171",
      },
      backgroundImage: {
        "brand-gradient":
          "linear-gradient(135deg, #5eead4 0%, #67e8f9 45%, #a78bfa 100%)",
        "accent-gradient":
          "linear-gradient(135deg, #5eead4 0%, #22d3ee 100%)",
        "surface-glow":
          "radial-gradient(120% 80% at 50% 0%, rgba(94, 234, 212, 0.06) 0%, rgba(167, 139, 250, 0.04) 35%, transparent 70%)",
      },
      boxShadow: {
        "soft":      "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 6px 24px -8px rgba(0,0,0,0.55)",
        "lift":      "0 1px 0 0 rgba(255,255,255,0.05) inset, 0 12px 36px -10px rgba(0,0,0,0.7)",
        "glow-accent": "0 0 0 1px rgba(94,234,212,0.35), 0 8px 30px -8px rgba(94,234,212,0.35)",
      },
      spacing: {
        "safe-top":   "env(safe-area-inset-top)",
        "safe-bot":   "env(safe-area-inset-bottom)",
        "safe-left":  "env(safe-area-inset-left)",
        "safe-right": "env(safe-area-inset-right)",
      },
      fontSize: {
        // Terminal-dense reading scale — slightly tighter than the default
        // Tailwind base. Used everywhere in the transcript.
        "tx":    ["13px", { lineHeight: "20px" }],
        "tx-sm": ["12px", { lineHeight: "18px" }],
        "tx-xs": ["11px", { lineHeight: "16px" }],
      },
      minHeight: { "touch": "44px" },
      minWidth:  { "touch": "44px" },
      animation: {
        "fade-in":     "fadeIn 180ms ease-out",
        "fade-in-up":  "fadeInUp 220ms ease-out",
        "pulse-soft":  "pulseSoft 2s ease-in-out infinite",
        "ambient":     "ambient 18s ease-in-out infinite",
        "shimmer":     "shimmer 2.4s linear infinite",
      },
      keyframes: {
        fadeIn:   {
          "0%":   { opacity: "0" },
          "100%": { opacity: "1" },
        },
        fadeInUp: {
          "0%":   { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        pulseSoft: {
          "0%, 100%": { opacity: "1" },
          "50%":      { opacity: "0.4" },
        },
        ambient: {
          "0%, 100%": { transform: "translate3d(0,0,0) scale(1)",     opacity: "0.55" },
          "50%":      { transform: "translate3d(2%,1%,0) scale(1.05)", opacity: "0.8"  },
        },
        shimmer: {
          "0%":   { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;

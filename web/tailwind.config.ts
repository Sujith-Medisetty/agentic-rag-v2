import type { Config } from "tailwindcss";

/**
 * Refined dark palette — warmer neutrals + one accent (teal) used sparingly.
 *
 *   bg        ▼ darkest layer, the page background
 *   surface     cards / panels sitting on top of bg
 *   elevated    inputs / hover states / nested cards inside surface
 *   border      hairline borders (subtle, not harsh)
 *   text        primary text (body)
 *   muted       secondary text (metadata, captions)
 *   subtle      tertiary text (timestamps, very low priority)
 *   accent      THE accent color — use sparingly for active state, send button
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
          "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont",
          "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif",
        ],
        mono: [
          "ui-monospace", "SFMono-Regular", "SF Mono", "Menlo",
          "Monaco", "Consolas", "Liberation Mono", "Courier New", "monospace",
        ],
      },
      colors: {
        bg:       "#0a0b0d",   // page
        surface:  "#111316",   // cards on bg
        elevated: "#181a1f",   // inputs / nested cards
        border:   "#23262d",   // hairlines
        text:     "#e8eaed",   // primary
        muted:    "#9097a3",   // secondary
        subtle:   "#5e6470",   // tertiary
        accent:   "#5eead4",   // ONE accent
        success:  "#34d399",
        warn:     "#fbbf24",
        danger:   "#f87171",
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
        // Subtle fade-in for new tool lines + new turn cards. 180ms is
        // perceptible without feeling slow.
        "fade-in":     "fadeIn 180ms ease-out",
        "fade-in-up":  "fadeInUp 220ms ease-out",
        // Pulsing dot during streaming. Gentle — 2s cycle so it doesn't
        // become a distraction.
        "pulse-soft":  "pulseSoft 2s ease-in-out infinite",
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
      },
    },
  },
  plugins: [],
} satisfies Config;

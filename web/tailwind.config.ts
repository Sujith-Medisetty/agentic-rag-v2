import type { Config } from "tailwindcss";

/**
 * Claude-inspired design system — warm cream + coral.
 *
 * Colors are wired through CSS variables (see src/index.css) so the
 * palette flips between light + dark automatically based on
 * prefers-color-scheme. To force a mode, set `data-theme="light"` or
 * `data-theme="dark"` on the root element (not currently wired but the
 * CSS variables already cover that path).
 *
 *   bg          page surface (warm cream in light, near-black in dark)
 *   surface     primary card surface (white in light)
 *   elevated    nested / hover surface (warmer cream)
 *   border      hairlines — low contrast on purpose
 *   text        primary body text
 *   muted       secondary text (metadata, captions)
 *   subtle      tertiary text (timestamps, very low priority)
 *   accent      coral — Claude's signature
 *   accent-2    warm tan (gradients / secondary accent)
 *   success     sage green
 *   warn        amber
 *   danger      rust
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          "Geist", "Inter", "ui-sans-serif", "system-ui",
          "-apple-system", "BlinkMacSystemFont", "Segoe UI",
          "Roboto", "Helvetica Neue", "Arial", "sans-serif",
        ],
        serif: [
          "Source Serif 4", "ui-serif", "Georgia", "Cambria",
          "Times New Roman", "serif",
        ],
        mono: [
          "Geist Mono", "ui-monospace", "SFMono-Regular", "SF Mono",
          "Menlo", "Monaco", "Consolas", "Liberation Mono",
          "Courier New", "monospace",
        ],
      },
      colors: {
        bg:        "hsl(var(--bg))",
        surface:   "hsl(var(--surface))",
        elevated:  "hsl(var(--elevated))",
        border:    "hsl(var(--border))",
        text:      "hsl(var(--text))",
        muted:     "hsl(var(--muted))",
        subtle:    "hsl(var(--subtle))",
        accent:    "hsl(var(--accent))",
        "accent-2": "hsl(var(--accent-2))",
        success:   "hsl(var(--success))",
        warn:      "hsl(var(--warn))",
        danger:    "hsl(var(--danger))",
      },
      backgroundImage: {
        "brand-gradient":
          "linear-gradient(135deg, hsl(var(--accent)) 0%, hsl(var(--accent-2)) 100%)",
        "accent-gradient":
          "linear-gradient(135deg, hsl(var(--accent)) 0%, hsl(var(--accent-2)) 100%)",
      },
      boxShadow: {
        // Warm soft shadows in light mode; deeper in dark.
        "soft":        "0 1px 2px 0 hsl(var(--shadow-soft) / 0.06), 0 1px 1px 0 hsl(var(--shadow-soft) / 0.04)",
        "lift":        "0 4px 12px -2px hsl(var(--shadow-lift) / 0.10), 0 2px 4px -1px hsl(var(--shadow-lift) / 0.06)",
        "glow-accent": "0 0 0 3px hsl(var(--accent) / 0.18)",
      },
      spacing: {
        "safe-top":   "env(safe-area-inset-top)",
        "safe-bot":   "env(safe-area-inset-bottom)",
        "safe-left":  "env(safe-area-inset-left)",
        "safe-right": "env(safe-area-inset-right)",
      },
      fontSize: {
        // Dense-but-readable scale for the transcript surface.
        "tx":    ["13px", { lineHeight: "20px" }],
        "tx-sm": ["12px", { lineHeight: "18px" }],
        "tx-xs": ["11px", { lineHeight: "16px" }],
      },
      minHeight: { "touch": "44px" },
      minWidth:  { "touch": "44px" },
      borderRadius: {
        // Slightly more generous defaults — fits the Claude warmth better.
        "lg":  "10px",
        "xl":  "14px",
        "2xl": "20px",
      },
      animation: {
        "fade-in":     "fadeIn 180ms ease-out",
        "fade-in-up":  "fadeInUp 220ms ease-out",
        "pulse-soft":  "pulseSoft 2s ease-in-out infinite",
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
        shimmer: {
          "0%":   { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;

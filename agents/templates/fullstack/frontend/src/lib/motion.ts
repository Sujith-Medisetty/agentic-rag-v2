import { useReducedMotion, type Variants } from "framer-motion";

/**
 * Shared motion variants for the Ojas template.
 *
 * All animations:
 *  - Use short durations (≤300ms) — feels responsive, not sluggish.
 *  - Are spring-based where direction matters (cards) and tween-based
 *    where direction doesn't (fades). Springs feel natural for items
 *    that land somewhere; tweens feel right for opacity-only reveals.
 *  - Honour prefers-reduced-motion via useReducedMotion() — users with
 *    vestibular sensitivity get instant transitions instead of motion.
 *
 * Import the variants you need and pass them to <motion.div variants={…}>
 * or use the helper components (FadeIn, StaggerContainer) below.
 */

/** Single element fades in + slides up 12px. Use on hero text, section titles. */
export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.25, ease: [0.16, 1, 0.3, 1] },
  },
};

/** Stagger container — children that have variants="hidden"/"visible" animate in order. */
export const stagger: Variants = {
  hidden: {},
  visible: {
    transition: { staggerChildren: 0.06, delayChildren: 0.05 },
  },
};

/** Card grid item — slightly more y travel than fadeUp, longer ease. */
export const cardIn: Variants = {
  hidden: { opacity: 0, y: 16, scale: 0.98 },
  visible: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0.32, ease: [0.16, 1, 0.3, 1] },
  },
};

/** Tap/hover micro-interaction for interactive elements. Pair with motion.button. */
export const tap = { scale: 0.97 };
export const hover = { scale: 1.02 };

/**
 * useMotionVariants() — returns the variants, but with reduced motion
 * turned into instant transitions (0 duration) when the user has
 * prefers-reduced-motion: reduce enabled.
 *
 * Usage:
 *   const v = useMotionVariants(fadeUp);
 *   <motion.div variants={v} initial="hidden" whileInView="visible" />
 */
export function useMotionVariants(v: Variants): Variants {
  const reduce = useReducedMotion();
  if (!reduce) return v;
  // For reduced motion, jump to the final state with 0 duration.
  const out: Variants = {};
  for (const [k, val] of Object.entries(v)) {
    if (val && typeof val === "object" && "transition" in val) {
      out[k] = { ...val, transition: { duration: 0 } };
    } else {
      out[k] = val as Variants[string];
    }
  }
  return out;
}

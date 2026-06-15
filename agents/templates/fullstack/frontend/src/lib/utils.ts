import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind class names safely — shadcn convention. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

/**
 * Defensive `.length` for values that might be `undefined`,
 * `null`, an object, a string, a number, or an array.
 *
 * Returns 0 for everything that is not a string or an array
 * with a numeric `.length`. Use this in render code that does:
 *
 *   const items = data?.items;          // could be undefined
 *   return <span>{items.length} items</span>   // throws!
 *
 * Replace with:
 *
 *   return <span>{safeLen(items)} items</span> // always 0+
 *
 * Why this exists: an Ojas sub-app that does `someApi.items.length`
 * without `Array.isArray` first will throw
 *   "Cannot read properties of undefined (reading 'length')"
 * in the browser console the moment the API returns a payload
 * shaped differently than the agent assumed (404 → `{}`,
 * 422 → `{detail: "..."}`, an upstream timeout → `null`).
 * The minified stack traces back to React's scheduler (the
 * "Y0e / ww / MessagePort.T" chain) and the whole subtree
 * unmounts, leaving the user staring at a partial page.
 *
 * The agent's prompt has a CRITICAL rule about this. `safeLen`
 * is the one-token escape hatch for code that already slipped
 * through review.
 */
export function safeLen(value: unknown): number {
  if (value == null) return 0;
  if (typeof value === "string" || Array.isArray(value)) {
    return value.length;
  }
  return 0;
}

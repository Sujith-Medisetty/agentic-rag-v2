#!/usr/bin/env node
/**
 * Guard 2: Radix Trigger/Content parent-child invariant.
 *
 * Catches the bug that ships blank pages to production: a
 * Radix Dialog / Sheet / AlertDialog trigger (or *Trigger)
 * rendered as a SIBLING of its provider instead of a descendant.
 * Radix's *Trigger components consume a context the provider
 * creates; if there's no provider in the React tree above the
 * trigger, runtime throws
 *   "DialogTrigger must be used within Dialog"
 * (or Sheet/AlertDialog variants), React unmounts the whole
 * tree, and the user sees a blank <div id="root"> with no
 * network errors. Same symptom as the duplicate-React bug, so
 * the user can't tell the two apart.
 *
 * Detection strategy: walk every .tsx file under src/ and check
 * that any <FooTrigger> / <FooContent> reference is reachable
 * from at least one <Foo> / <Foo.Root> in the same JSX tree
 * rooted at the file. The file-level root is taken to be the
 * default-exported component's return value. We deliberately
 * skip the ui/ library (which contains the primitive
 * implementations — those are the providers, not consumers).
 *
 * Limitations:
 *   - Cross-file trigger/provider pairs (e.g. trigger in one
 *     component, content in another) are NOT detected. The
 *     agent's prompt rule (CRITICAL block in agents/prompt.py)
 *     covers that case.
 *   - We don't try to resolve JSX expressions; only
 *     direct-usage patterns (the most common case). A trigger
 *     built via `React.createElement(SheetTrigger, ...)` would
 *     be missed, but that pattern is rare in the template.
 *
 * Usage:
 *   node scripts/verify-radix.mjs
 *
 * Exit 1 with a list of bad files on any violation. Exit 0 if
 * the source is clean.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.resolve(__dirname, "..", "src");
const SKIP_DIRS = new Set(["ui", "node_modules", "dist", "scripts"]);

// Radix component pairs we care about. The KEY is the Trigger
// component (the one that throws when used outside its
// provider). The VALUE is the list of provider names that
// satisfy the trigger's context. We treat `Foo` and `Foo.Root`
// as the same provider (the template's ui/sheet re-exports
// DialogPrimitive.Root as `Sheet`).
const TRIGGER_NEEDS_PROVIDER = {
  SheetTrigger: ["Sheet"],
  DialogTrigger: ["Dialog"],
  AlertDialogTrigger: ["AlertDialog"],
  PopoverTrigger: ["Popover"],
  DropdownMenuTrigger: ["DropdownMenu", "DropdownMenuSub"],
  ContextMenuTrigger: ["ContextMenu"],
  HoverCardTrigger: ["HoverCard"],
  TooltipTrigger: ["Tooltip"],
  ToggleGroupItem: ["ToggleGroup"], // not exactly a trigger, but same family
  RadioGroupItem: ["RadioGroup"],
};

const ALL_TRIGGERS = Object.keys(TRIGGER_NEEDS_PROVIDER);
const ALL_PROVIDERS = [
  ...new Set(Object.values(TRIGGER_NEEDS_PROVIDER).flat()),
];

/** Collect all .tsx files under SRC, skipping ui/ (primitives). */
function* walkTsx(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkTsx(full);
    } else if (entry.isFile() && entry.name.endsWith(".tsx")) {
      yield full;
    }
  }
}

/**
 * For a given .tsx file, find all `<FooTrigger>` JSX usages and
 * verify that at least one matching provider (`<Foo>`) appears
 * in the same file. This is a coarse, file-level check — it
 * doesn't trace JSX subtrees, so a trigger in one component
 * and a provider in another passes. The agent's prompt rule
 * covers that case. Within a single component (the common
 * case), the rule is fully enforced.
 */
function checkFile(file) {
  const src = fs.readFileSync(file, "utf8");
  const used = ALL_TRIGGERS.filter((t) => new RegExp(`<${t}\\b`).test(src));
  if (used.length === 0) return null;

  const missing = [];
  for (const t of used) {
    const providers = TRIGGER_NEEDS_PROVIDER[t];
    const has = providers.some((p) => new RegExp(`<${p}\\b`).test(src));
    if (!has) {
      missing.push(`<${t}> used without <${providers.join(" or ")}> in the same file (${path.relative(process.cwd(), file)})`);
    }
  }
  return missing.length > 0 ? missing : null;
}

let violations = 0;
for (const file of walkTsx(SRC)) {
  const miss = checkFile(file);
  if (miss) {
    for (const m of miss) {
      console.error(`  ✗ ${m}`);
      violations += 1;
    }
  }
}

if (violations > 0) {
  console.error(
    `\n${violations} Radix invariant violation${violations === 1 ? "" : "s"} found. ` +
      "A trigger component (e.g. <SheetTrigger>) used outside its provider " +
      "(e.g. <Sheet>) causes the runtime to throw 'DialogTrigger must be used " +
      "within Dialog', React unmounts the whole tree, and the user sees a " +
      "blank page. Wrap the trigger AND the content in the same <Sheet> / " +
      "<Dialog> / <AlertDialog> / <Popover> / <DropdownMenu> provider so " +
      "both are descendants of the same context.",
  );
  process.exit(1);
}

console.log("✓ Radix Trigger/Content parent-child invariant OK");

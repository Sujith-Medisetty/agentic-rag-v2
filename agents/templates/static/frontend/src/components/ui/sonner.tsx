import { useTheme } from "next-themes";
import { Toaster as Sonner, type ToasterProps } from "sonner";

/**
 * Sonner toaster wired to the Ojas design tokens. Place once at the
 * top of the app. Use `toast.success(...)` / `toast.error(...)` from
 * anywhere in the tree.
 *
 * The `theme` prop is now bound to next-themes' resolved theme (which
 * already accounts for system preference + explicit override), so the
 * toast colors switch automatically when the user toggles light/dark.
 * Previously it was hardcoded to "system" which read the OS preference
 * but did NOT respect the user's manual override.
 *
 * Positioning notes:
 *  - `position="top-right"` keeps toasts out of the way of chat / forms.
 *  - `offset` accounts for the sticky top nav so toasts don't sit behind it.
 *  - `mobileOffset` keeps the toast above the iOS home indicator.
 *  - `richColors` is OFF so the design tokens (--background, --border) win
 *    over Sonner's default color palette. Without this, toasts get a
 *    jarring blue/green/red scheme that doesn't match the rest of the UI.
 */
const Toaster = ({ ...props }: ToasterProps) => {
  const { resolvedTheme = "system" } = useTheme();
  return (
    <Sonner
      theme={resolvedTheme as ToasterProps["theme"]}
      className="toaster group"
      position="top-right"
      offset="14px"
      mobileOffset="14px"
      expand
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          actionButton:
            "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
          success:
            "group-[.toaster]:bg-success group-[.toaster]:text-success-foreground group-[.toaster]:border-success",
          error:
            "group-[.toaster]:bg-destructive group-[.toaster]:text-destructive-foreground group-[.toaster]:border-destructive",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };

import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { motion } from "framer-motion";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

// ─── Module-scoped event capture ───────────────────────────────────────
// The browser fires `beforeinstallprompt` once per page load, and only
// when the install criteria are met. We capture it at module import
// time so ANY component rendered anywhere in the tree can call .prompt()
// via the `deferred` ref. Listeners are notified on the appinstalled
// event so we can hide the button.
interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

let deferred: BeforeInstallPromptEvent | null = null;
const listeners = new Set<() => void>();

if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e as BeforeInstallPromptEvent;
    listeners.forEach((l) => l());
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    listeners.forEach((l) => l());
  });
}

function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    // iOS Safari exposes this when launched from the home screen
    (window.navigator as unknown as { standalone?: boolean }).standalone ===
      true
  );
}

function isIOS(): boolean {
  if (typeof navigator === "undefined") return false;
  return /iPad|iPhone|iPod/.test(navigator.userAgent) && !/MSStream/.test(navigator.userAgent);
}

function useInstallState() {
  const [, force] = useState(0);
  useEffect(() => {
    const cb = () => force((n) => n + 1);
    listeners.add(cb);
    return () => {
      listeners.delete(cb);
    };
  }, []);
  return {
    canPrompt: deferred !== null,
    installed: isStandalone(),
  };
}

/**
 * PWA install button. Renders nothing once the app is installed.
 * On Chrome/Edge, triggers the native install prompt. On iOS Safari
 * (or any browser that never fires beforeinstallprompt), opens a
 * modal with platform-specific instructions.
 */
export default function InstallButton() {
  const { canPrompt, installed } = useInstallState();
  const [hintOpen, setHintOpen] = useState(false);

  if (installed) return null;

  const handleClick = async () => {
    if (deferred) {
      await deferred.prompt();
      const choice = await deferred.userChoice;
      if (choice.outcome === "accepted") {
        toast.success("Installed — find it on your home screen.");
      }
      deferred = null;
      return;
    }
    // No native prompt available (iOS, Firefox, etc.) — show hint.
    setHintOpen(true);
  };

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <motion.div
            whileTap={{ scale: 0.95 }}
            transition={{ type: "spring", stiffness: 400, damping: 17 }}
            className="inline-flex"
          >
            <Button
              variant="outline"
              size="sm"
              onClick={handleClick}
              aria-label="Install app"
              className="gap-2"
            >
              <Download className="h-4 w-4" />
              Install
            </Button>
          </motion.div>
        </TooltipTrigger>
        <TooltipContent>
          {canPrompt ? "Add to home screen" : "Install instructions"}
        </TooltipContent>
      </Tooltip>

      <Dialog open={hintOpen} onOpenChange={setHintOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Install this app</DialogTitle>
            <DialogDescription>
              {isIOS() ? (
                <>
                  On iPhone or iPad: tap the{" "}
                  <span className="font-medium text-foreground">Share</span>{" "}
                  button, then{" "}
                  <span className="font-medium text-foreground">
                    Add to Home Screen
                  </span>
                  .
                </>
              ) : (
                <>
                  Open your browser menu (⋮ or ⌘-shift-A) and choose{" "}
                  <span className="font-medium text-foreground">
                    Install app
                  </span>{" "}
                  or{" "}
                  <span className="font-medium text-foreground">
                    Add to Home Screen
                  </span>
                  .
                </>
              )}
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end">
            <Button onClick={() => setHintOpen(false)}>Got it</Button>
          </div>
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  );
}

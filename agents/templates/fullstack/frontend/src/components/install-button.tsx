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
import { pwaInstall } from "@/lib/pwa-install";
import { hover, tap } from "@/lib/motion";

function useInstallState() {
  // Force a re-render whenever the module-scoped event listener fires.
  const [, force] = useState(0);
  useEffect(() => pwaInstall.subscribe(() => force((n) => n + 1)), []);
  return {
    canPrompt: pwaInstall.getDeferred() !== null,
    installed: pwaInstall.isStandalone(),
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
    const deferred = pwaInstall.getDeferred();
    if (deferred) {
      await deferred.prompt();
      const choice = await deferred.userChoice;
      if (choice.outcome === "accepted") {
        toast.success("Installed — find it on your home screen.");
      }
      pwaInstall.clearDeferred();
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
            whileHover={hover}
            whileTap={tap}
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
              {pwaInstall.isIOS() ? (
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

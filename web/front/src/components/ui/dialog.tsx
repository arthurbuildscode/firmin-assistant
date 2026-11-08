// shadcn/ui — Dialog et Sheet (MIT), sur Radix Dialog. Feuille basse sur mobile, fenêtre centrée sur desktop.
import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;
export const DialogTitle = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Title>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Title ref={ref} className={cn("text-base font-semibold leading-snug", className)} {...props} />
));
DialogTitle.displayName = "DialogTitle";
export const DialogDescription = React.forwardRef<
  React.ElementRef<typeof DialogPrimitive.Description>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Description ref={ref} className={cn("text-sm text-muted-foreground", className)} {...props} />
));
DialogDescription.displayName = "DialogDescription";

type ProprietesContenu = React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
  /** « feuille » : bas d'écran sur mobile, fenêtre au centre sur desktop. */
  forme?: "feuille" | "fenetre";
  fermer?: boolean;
};

export const DialogContent = React.forwardRef<React.ElementRef<typeof DialogPrimitive.Content>, ProprietesContenu>(
  ({ className, children, forme = "feuille", fermer = true, ...props }, ref) => (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-[60] bg-[var(--overlay)] animate-[fondu_.2s_ease-out]" />
      <DialogPrimitive.Content
        ref={ref}
        className={cn(
          "fixed z-[61] flex flex-col gap-3.5 overflow-y-auto border border-border bg-popover text-popover-foreground outline-none",
          forme === "feuille"
            ? "inset-x-0 bottom-0 max-h-[88dvh] rounded-t-2xl border-b-0 px-4 pt-3 pb-[calc(20px+env(safe-area-inset-bottom,0px))] " +
                "lg:inset-x-auto lg:bottom-auto lg:left-1/2 lg:top-1/2 lg:w-[min(500px,calc(100vw-48px))] lg:-translate-x-1/2 lg:-translate-y-1/2 lg:rounded-xl lg:border-b lg:p-5"
            : "left-1/2 top-1/2 w-[min(440px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 rounded-xl p-5 max-h-[88dvh]",
          className,
        )}
        {...props}
      >
        {forme === "feuille" && <div aria-hidden className="mx-auto mb-1 h-1 w-10 rounded bg-border lg:hidden" />}
        {children}
        {fermer && (
          <DialogPrimitive.Close
            className="absolute right-3 top-3 grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground cursor-pointer"
            aria-label="Fermer"
          >
            <X className="size-4" />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  ),
);
DialogContent.displayName = "DialogContent";

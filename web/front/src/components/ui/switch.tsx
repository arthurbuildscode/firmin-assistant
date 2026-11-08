// shadcn/ui — Switch (MIT), sur Radix.
import * as React from "react";
import * as SwitchPrimitive from "@radix-ui/react-switch";
import { cn } from "@/lib/utils";

export const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitive.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitive.Root
    ref={ref}
    className={cn(
      "relative inline-flex h-[22px] w-[38px] shrink-0 cursor-pointer items-center rounded-full border-0 p-0 transition-colors " +
        "bg-input data-[state=checked]:bg-primary disabled:cursor-not-allowed disabled:opacity-50",
      className,
    )}
    {...props}
  >
    <SwitchPrimitive.Thumb
      className={
        "pointer-events-none block size-4 translate-x-[3px] rounded-full bg-background shadow-[0_1px_2px_rgb(0_0_0/.25)] " +
        "transition-transform data-[state=checked]:translate-x-[19px]"
      }
    />
  </SwitchPrimitive.Root>
));
Switch.displayName = "Switch";

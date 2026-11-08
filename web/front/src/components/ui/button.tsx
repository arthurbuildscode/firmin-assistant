// shadcn/ui — Button (MIT), adapté aux jetons de la maquette.
import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const variantesBouton = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md border text-sm font-medium leading-none cursor-pointer " +
    "transition-[background-color,border-color,color,opacity] select-none [-webkit-tap-highlight-color:transparent] " +
    "disabled:pointer-events-none disabled:opacity-45 active:translate-y-px [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground border-transparent hover:opacity-90",
        outline: "bg-card text-foreground border-border hover:bg-muted",
        secondary: "bg-secondary text-secondary-foreground border-transparent hover:bg-muted",
        ghost: "bg-transparent border-transparent hover:bg-muted",
        destructive: "bg-danger text-white border-transparent hover:opacity-90",
        link: "border-transparent underline-offset-4 hover:underline px-0",
      },
      size: {
        default: "h-9 px-3.5",
        sm: "h-8 px-3",
        lg: "h-11 px-4.5 text-base",
        icon: "size-9 p-0",
        "icon-sm": "size-8 p-0",
      },
    },
    defaultVariants: { variant: "outline", size: "default" },
  },
);

export interface ProprietesBouton
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof variantesBouton> {
  asChild?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ProprietesBouton>(
  ({ className, variant, size, asChild = false, type, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        type={asChild ? undefined : (type ?? "button")}
        className={cn(variantesBouton({ variant, size }), className)}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

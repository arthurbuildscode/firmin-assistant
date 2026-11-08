// shadcn/ui — Badge (MIT), variantes de la maquette.
import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const variantesBadge = cva(
  "inline-flex items-center gap-1.5 h-[22px] px-2.5 rounded-full text-xs font-medium leading-none whitespace-nowrap border border-transparent [&_svg]:size-3",
  {
    variants: {
      variant: {
        default: "bg-secondary text-secondary-foreground",
        solid: "bg-primary text-primary-foreground",
        soft: "bg-primary-soft text-foreground",
        outline: "bg-transparent border-border",
        danger: "bg-danger-bg text-danger",
        ok: "bg-transparent text-muted-foreground px-0 h-auto text-sm font-normal",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof variantesBadge>) {
  return <span className={cn(variantesBadge({ variant }), className)} {...props} />;
}

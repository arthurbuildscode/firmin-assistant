// shadcn/ui — Tooltip (MIT) : info-bulles des boutons-icônes (le libellé reste aussi en aria-label).
import * as React from "react";
import * as T from "@radix-ui/react-tooltip";

export const FournisseurInfoBulles = ({ children }: { children: React.ReactNode }) => (
  <T.Provider delayDuration={400}>{children}</T.Provider>
);

export function InfoBulle({ texte, children }: { texte: string; children: React.ReactElement }) {
  return (
    <T.Root>
      <T.Trigger asChild>{children}</T.Trigger>
      <T.Portal>
        <T.Content
          sideOffset={6}
          className="z-[75] max-w-[260px] rounded-md bg-foreground px-2.5 py-1.5 text-xs text-background animate-[apparait_.12s_ease-out]"
        >
          {texte}
        </T.Content>
      </T.Portal>
    </T.Root>
  );
}

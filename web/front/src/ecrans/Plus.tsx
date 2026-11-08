// « Plus » (mobile) : le reste de la navigation.
import { ChevronRight } from "lucide-react";
import { ECRANS_INFO } from "@/components/Coquille";
import { naviguer, type Ecran } from "@/lib/routeur";
import { useAssistant } from "@/lib/session";
import { TitrePage } from "./commun";

const LIENS: [Ecran, (assistant: string) => string][] = [
  ["echeances", () => "Et anniversaires"],
  ["journal", () => "Souvenirs du mois"],
  ["fichiers", () => "Documents rangés et sensibles"],
  ["memoire", (a) => `Ce que sait ${a}`],
  ["reglages", () => "Profil, palette, voix, appareils"],
  ["routines", (a) => `Ce que ${a} fait en tâche de fond`],
];

export function EcranPlus() {
  const assistant = useAssistant();
  return (
    <>
      <TitrePage titre="Plus" />
      <div className="flex max-w-[720px] flex-col rounded-lg border px-4">
        {LIENS.map(([e, sous]) => {
          const I = ECRANS_INFO[e].icone;
          return (
            <a key={e} className="lien-plus" href={`/${e}`} onClick={(ev) => { ev.preventDefault(); naviguer(e); }}>
              <I className="size-[18px] text-muted-foreground" />
              <span className="min-w-0 flex-1">
                <span className="ligne-t block">{ECRANS_INFO[e].libelle}</span>
                <span className="ligne-s block">{sous(assistant)}</span>
              </span>
              <ChevronRight className="size-4 text-muted-foreground" />
            </a>
          );
        })}
      </div>
    </>
  );
}

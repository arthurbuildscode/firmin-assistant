// Éléments communs aux écrans dédiés : titre, demande d'actualisation par la conversation (repli tant que le point
// d'accès direct de l'écran n'existe pas), état vide sobre. Un écran montre ses données, jamais un extrait de
// conversation : la réponse à une demande faite depuis l'écran tient sur une ligne dans le dock.
import type { ReactNode } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { naviguer, type Ecran } from "@/lib/routeur";
import { magasin, useConversation, type Contexte } from "@/store/conversation";

export function TitrePage({ titre, sous, droite }: { titre: string; sous?: ReactNode; droite?: ReactNode }) {
  return (
    <div className="titre-page">
      <div className="min-w-0">
        <h1>{titre}</h1>
        {sous && <p className="sous">{sous}</p>}
      </div>
      {droite && <div className="flex flex-wrap items-center gap-2">{droite}</div>}
    </div>
  );
}

/** Demande à l'assistant de (re)produire les cartes de l'écran ; la réponse s'affiche sur l'écran. */
export function BoutonActualiser({ texte, contexte, libelle = "Actualiser" }: { texte: string; contexte: Contexte; libelle?: string }) {
  const enCours = useConversation((e) => e.echangeEnCours);
  return (
    <Button variant="outline" size="sm" disabled={enCours} onClick={() => void magasin.envoyer(texte, { contexte })}>
      <RefreshCw className={enCours ? "animate-spin" : ""} />
      {libelle}
    </Button>
  );
}

export function Vide({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="vide flex flex-col items-start gap-3">
      <div>{children}</div>
      {action}
    </div>
  );
}

/** Lien discret de bas d'écran (« Routines courses › »). */
export function LienBas({ ecran, children }: { ecran: Ecran; children: ReactNode }) {
  return (
    <a
      className="lien-bas"
      href={`/${ecran}`}
      onClick={(e) => {
        e.preventDefault();
        naviguer(ecran);
      }}
    >
      {children}
      <ChevronRight className="size-3.5" aria-hidden />
    </a>
  );
}

/** Jour et mois en colonne (échéances, journal) : « 16 / nov. / J-6 ». */
export function DateColonne({ jour, sous, petit }: { jour: ReactNode; sous?: ReactNode; petit?: ReactNode }) {
  return (
    <div className="date-col">
      <span className="j num">{jour}</span>
      {sous && <span className="m">{sous}</span>}
      {petit && <span className="m num">{petit}</span>}
    </div>
  );
}

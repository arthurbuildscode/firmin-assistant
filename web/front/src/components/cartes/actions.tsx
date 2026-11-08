// Boutons des cartes (CONTRACT §14) : aucune action n'exécute quoi que ce soit sans passer par l'API
// (confirmation, step-up pour le sensible).
import { createContext, useContext, useState } from "react";
import { Check, Fingerprint, Loader2, Volume2, Square, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { InfoBulle } from "@/components/ui/tooltip";
import { api, messageErreur } from "@/lib/api";
import { ecranCourant, naviguer } from "@/lib/routeur";
import { toast } from "@/lib/toasts";
import type { ActionCarte, Carte, Confirmation, Decision } from "@/lib/types";
import { arreterLecture, lireAVoixHaute, useLecture } from "@/lib/voix";
import { magasin, useConversation } from "@/store/conversation";
import { demanderOuverture } from "@/store/sensibles";

export interface ContexteCarte {
  cleMessage?: string;
  /** Rendu dans un écran dédié (pas dans le fil) : cartes plus larges, pas de méta. */
  ecran?: string;
}

export const ContexteCarteReact = createContext<ContexteCarte>({});
export const useContexteCarte = () => useContext(ContexteCarteReact);

/** Texte lisible d'une carte pour la lecture à voix haute. */
export function texteDeCarte(c: Carte): string {
  const d = c.donnees || {};
  if (typeof d.texte === "string") return d.texte;
  const morceaux: string[] = [];
  if (c.titre) morceaux.push(c.titre);
  if (Array.isArray(d.evenements))
    for (const e of d.evenements) morceaux.push(`${e.journee_entiere ? "Toute la journée" : String(e.debut || "").slice(11, 16).replace(":", " heures ")} : ${e.titre}`);
  if (Array.isArray(d.elements)) for (const e of d.elements.slice(0, 6)) morceaux.push(String(e.titre || ""));
  return morceaux.join(". ");
}

export async function executerAction(a: ActionCarte, carte: Carte): Promise<void> {
  const args = (a.args || {}) as Record<string, unknown>;
  switch (a.action) {
    case "message":
      await magasin.envoyer(String(args.texte || a.libelle), { contexte: { ecran: ecranCourant(), objet: carte.titre || carte.type } });
      return;
    case "confirmer":
      if (typeof a.confirmation === "number") await magasin.decider(a.confirmation, args.decision === "annuler" ? "annuler" : "valider");
      return;
    case "ouvrir":
      naviguer(String(args.ecran || "conversation"));
      return;
    case "ecouter":
      try {
        await lireAVoixHaute(String(args.texte || texteDeCarte(carte)), carte.id);
      } catch (e) {
        toast(messageErreur(e));
      }
      return;
    case "deverrouiller":
      // Document sensible (carte `document_sensible`, `args.id`) : écran Fichiers, empreinte, document ouvert.
      naviguer("fichiers");
      await demanderOuverture(typeof args.id === "string" ? args.id : undefined);
      return;
    case "routine": {
      const r = await api<{ confirmation: Confirmation }>("PATCH", `/api/routines/${encodeURIComponent(String(args.capacite))}/${encodeURIComponent(String(args.nom))}`, {
        quand: String(args.quand || ""),
      });
      magasin.ajouterConfirmationLocale(r.confirmation);
      toast("Changement préparé : à valider.");
      return;
    }
    case "lien":
      // Liens http(s) seulement (le cœur écarte déjà les autres schémas).
      if (typeof args.url === "string" && /^https?:\/\//i.test(args.url)) window.open(args.url, "_blank", "noopener,noreferrer");
      return;
    default:
      return;
  }
}

/** Haut-parleur seul (règle UX : « Écouter » en info-bulle et aria-label), identique partout. */
export function BoutonEcouter({ texte, cle, className, variante }: { texte: string; cle: string; className?: string; variante?: "mini" }) {
  const lecture = useLecture();
  const actif = lecture === cle;
  const basculer = () => {
    if (actif) arreterLecture();
    else lireAVoixHaute(texte, cle).catch((e) => toast(messageErreur(e)));
  };
  if (variante === "mini")
    return (
      <InfoBulle texte={actif ? "Arrêter la lecture" : "Écouter"}>
        <button type="button" className={className} aria-label={actif ? "Arrêter la lecture" : "Écouter"} aria-pressed={actif} onClick={basculer}>
          {actif ? <Square /> : <Volume2 />}
        </button>
      </InfoBulle>
    );
  return (
    <InfoBulle texte={actif ? "Arrêter la lecture" : "Écouter"}>
      <Button
        variant="outline"
        size="icon"
        className={className}
        aria-label={actif ? "Arrêter la lecture" : "Écouter"}
        aria-pressed={actif}
        onClick={() => {
          if (actif) arreterLecture();
          else lireAVoixHaute(texte, cle).catch((e) => toast(messageErreur(e)));
        }}
      >
        {actif ? <Square /> : <Volume2 />}
      </Button>
    </InfoBulle>
  );
}

export function EtatDecision({ decision, auto }: { decision: Decision | "en_cours"; auto?: boolean }) {
  if (decision === "en_cours")
    return (
      <Badge variant="ok">
        <Loader2 className="animate-spin" />
        Un instant…
      </Badge>
    );
  return (
    <Badge variant="ok">
      {decision === "valider" ? <Check /> : <X />}
      {decision === "valider" ? (auto ? "Fait automatiquement" : "Validé") : "Annulé"}
    </Badge>
  );
}

/** Boutons d'une carte ; une fois la proposition décidée, l'état remplace les boutons sur place. */
export function BoutonsCarte({ carte, exclure = [] }: { carte: Carte; exclure?: string[] }) {
  const decisions = useConversation((e) => e.decisions);
  const [occupe, setOccupe] = useState<number | null>(null);
  const actions = (carte.actions || []).filter((a) => !exclure.includes(a.action));
  if (!actions.length) return null;
  const ids = actions.map((a) => a.confirmation).filter((x): x is number => typeof x === "number");
  const decision = ids.map((id) => decisions[id]).find(Boolean);
  if (decision && decision !== "en_cours") {
    const reste = actions.filter((a) => a.confirmation === undefined || a.confirmation === null).filter((a) => a.action !== "message");
    return (
      <div className="boutons items-center">
        <EtatDecision decision={decision} />
        {reste.map((a, i) => (
          <Button key={i} size="sm" variant="ghost" onClick={() => executerAction(a, carte).catch((e) => toast(messageErreur(e)))}>
            {a.libelle}
          </Button>
        ))}
      </div>
    );
  }
  let premierPrincipal = true;
  return (
    <div className="boutons">
      {actions.map((a, i) => {
        const valider = a.action === "confirmer" && (a.args as { decision?: string } | undefined)?.decision !== "annuler";
        const annuler = a.action === "confirmer" && !valider;
        const principal = valider && premierPrincipal;
        if (principal) premierPrincipal = false;
        return (
          <Button
            key={i}
            size="sm"
            variant={principal ? "default" : annuler ? "ghost" : i === 0 ? "secondary" : "outline"}
            disabled={decision === "en_cours" || occupe === i}
            onClick={async () => {
              setOccupe(i);
              try {
                await executerAction(a, carte);
              } catch (e) {
                toast(messageErreur(e));
              } finally {
                setOccupe(null);
              }
            }}
          >
            {occupe === i && <Loader2 className="animate-spin" />}
            {a.libelle}
          </Button>
        );
      })}
    </div>
  );
}

/** Confirmation sans carte (CONFIRMATION_REQUISE dans la réponse) : Valider / Modifier / Annuler explicites. */
export function ConfirmationEnLigne({ c }: { c: Confirmation }) {
  const decision = useConversation((e) => e.decisions[c.id]);
  if (c.auto) {
    return (
      <div className="carte gap-2">
        <div className="text-sm">{c.resume}</div>
        <EtatDecision decision="valider" auto />
      </div>
    );
  }
  return (
    <div className="carte gap-3" data-confirmation={c.id}>
      <div className="text-sm leading-snug">{c.resume}</div>
      {decision && decision !== "en_cours" ? (
        <EtatDecision decision={decision} />
      ) : (
        <div className="boutons">
          <Button size="sm" variant="default" disabled={decision === "en_cours"} onClick={() => magasin.decider(c.id, "valider")}>
            {c.step_up && <Fingerprint />}
            Valider
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={decision === "en_cours"}
            onClick={() => magasin.envoyer("Je voudrais modifier cette proposition.", { contexte: { ecran: ecranCourant(), objet: c.resume } })}
          >
            Modifier
          </Button>
          <Button size="sm" variant="ghost" disabled={decision === "en_cours"} onClick={() => magasin.decider(c.id, "annuler")}>
            Annuler
          </Button>
        </div>
      )}
    </div>
  );
}

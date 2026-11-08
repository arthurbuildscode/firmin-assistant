// Cartes de la capacité transports (CONTRACT §14) : prochains départs d'un ou plusieurs trajets, état d'un train ou
// d'un vol. Compactes : l'alerte d'abord (s'il y en a une), puis deux ou trois départs ; le détail (arrêts) au geste.
import { Bus, Plane, TrainFront, TramFront, TriangleAlert } from "lucide-react";
import type { ComponentType } from "react";
import { Badge } from "@/components/ui/badge";
import type { Carte } from "@/lib/types";
import { BoutonsCarte } from "./actions";

type Depart = {
  heure_theorique?: string;
  heure_estimee?: string | null;
  retard_min?: number | null;
  dans_min?: number | null;
  statut?: string;
  resume?: string;
  quai?: string | null;
};
type Trajet = {
  titre?: string;
  mode?: string;
  alerte?: string | null;
  avertissement?: string | null;
  departs?: Depart[];
  perturbations?: { titre?: string }[];
};
type Arret = { lieu?: string; prevu?: string; estime?: string | null; retard_min?: number | null };

const ICONES: Record<string, ComponentType<{ className?: string }>> = {
  train: TrainFront,
  metro: TramFront,
  tram: TramFront,
  avion: Plane,
};

function IconeMode({ mode }: { mode?: string }) {
  const Icone = ICONES[mode || ""] ?? Bus;
  return <Icone className="size-4 shrink-0 text-muted-foreground" />;
}

function quand(d: Depart): string {
  if (d.statut === "supprime") return "supprimé";
  if (d.statut === "a_l_arret") return "à l’arrêt";
  if (typeof d.dans_min === "number" && d.dans_min >= 0 && d.dans_min <= 60) return `dans ${d.dans_min} min`;
  return d.heure_estimee || d.heure_theorique || "";
}

function precision(d: Depart): string {
  if (d.statut === "supprime") return `prévu à ${d.heure_theorique ?? ""}`;
  if (d.heure_estimee == null) return `${d.heure_theorique ?? ""} · théorique`;
  const retard = d.retard_min ?? 0;
  const etat = retard > 0 ? `+${retard} min` : retard < 0 ? `${retard} min` : "à l’heure";
  return [d.heure_estimee, etat, d.quai ? `voie ${d.quai}` : ""].filter(Boolean).join(" · ");
}

export function CarteTransportsProchains({ carte }: { carte: Carte }) {
  const trajets: Trajet[] = Array.isArray(carte.donnees.trajets) ? carte.donnees.trajets : [];
  const plusieurs = trajets.length > 1;
  return (
    <section className="carte" data-carte="transports_prochains">
      {!plusieurs && (
        <div className="carte-titre flex items-center gap-2">
          <IconeMode mode={trajets[0]?.mode} />
          <span className="min-w-0">{carte.titre || trajets[0]?.titre || "Prochains départs"}</span>
        </div>
      )}
      {trajets.map((t, i) => {
        const departs = (t.departs ?? []).slice(0, 3);
        return (
          <div className="flex flex-col gap-2 min-w-0" key={i}>
            {plusieurs && (
              <div className="flex items-center gap-2 text-sm font-medium min-w-0">
                <IconeMode mode={t.mode} />
                <span className="min-w-0">{t.titre}</span>
              </div>
            )}
            {t.alerte && (
              <p className="flex items-start gap-2 text-sm text-danger" role="status">
                <TriangleAlert className="size-4 shrink-0 mt-0.5" />
                <span>{t.alerte.charAt(0).toUpperCase() + t.alerte.slice(1)}</span>
              </p>
            )}
            {departs.map((d, j) => (
              <div className={`ligne-carte${d.statut === "supprime" ? " line-through text-muted-foreground" : ""}`} key={j}>
                <span className="num font-medium">{quand(d)}</span>
                <span className={`num ${d.retard_min ? "" : "text-muted-foreground"}`}>{precision(d)}</span>
              </div>
            ))}
            {!departs.length && <p className="text-sm text-muted-foreground">Aucun départ dans l’heure.</p>}
            {(t.perturbations ?? []).slice(0, 1).map((p, j) => (
              <p className="text-xs text-muted-foreground" key={j}>{p.titre}</p>
            ))}
            {t.avertissement && <p className="text-xs text-muted-foreground">{t.avertissement}</p>}
          </div>
        );
      })}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

const STATUTS: Record<string, { libelle: string; variante: "danger" | "default" | "outline" }> = {
  supprime: { libelle: "Supprimé", variante: "danger" },
  en_retard: { libelle: "En retard", variante: "danger" },
  a_l_heure: { libelle: "À l’heure", variante: "default" },
  prevu: { libelle: "Prévu", variante: "outline" },
  inconnu: { libelle: "Pas de suivi", variante: "outline" },
};

export function CarteTransportsEtat({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const statut = STATUTS[String(d.statut)] ?? STATUTS.inconnu;
  const depart: Arret = d.depart || {};
  const arrivee: Arret = d.arrivee || {};
  const arrets: Arret[] = Array.isArray(d.arrets) ? d.arrets : [];
  const perturbations: { titre?: string; texte?: string }[] = Array.isArray(d.perturbations) ? d.perturbations : [];
  const heure = (a: Arret) => (a.estime && a.estime !== a.prevu ? `${a.estime} (prévu ${a.prevu})` : a.prevu);
  return (
    <section className="carte" data-carte="transports_etat">
      <div className="flex items-center justify-between gap-3 min-w-0">
        <div className="carte-titre flex items-center gap-2 min-w-0">
          <IconeMode mode={String(d.mode || "")} />
          <span>{carte.titre || String(d.numero)}</span>
        </div>
        <Badge variant={statut.variante}>
          {statut.libelle}
          {d.statut === "en_retard" && typeof d.retard_min === "number" ? ` · +${d.retard_min} min` : ""}
        </Badge>
      </div>
      {depart.lieu && (
        <div className="flex flex-col gap-1">
          <div className="ligne-carte">
            <span>{depart.lieu}</span>
            <span className="num">{heure(depart)}</span>
          </div>
          {arrivee.lieu && (
            <div className="ligne-carte">
              <span>{arrivee.lieu}</span>
              <span className="num">{heure(arrivee)}</span>
            </div>
          )}
        </div>
      )}
      {d.message && <p className="text-sm text-muted-foreground">{String(d.message)}</p>}
      {perturbations.slice(0, 2).map((p, i) => (
        <p className="flex items-start gap-2 text-sm" key={i}>
          <TriangleAlert className="size-4 shrink-0 mt-0.5 text-danger" />
          <span>{p.titre}</span>
        </p>
      ))}
      {arrets.length > 2 && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">{arrets.length} arrêts</summary>
          <div className="mt-2 flex flex-col gap-1">
            {arrets.map((a, i) => (
              <div className="ligne-carte" key={i}>
                <span>{a.lieu}</span>
                <span className={`num ${a.retard_min ? "" : "text-muted-foreground"}`}>{heure(a)}</span>
              </div>
            ))}
          </div>
        </details>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

// Semaine : sept jours glissants à partir d'aujourd'hui (GET /api/brief/jour par jour, sans le modèle ; consultable
// jusqu'à J+14 ; une semaine du lundi au dimanche attend un point d'accès qui lise aussi les jours passés). Les
// événements privés de l'autre adulte n'arrivent jamais ici (filtrés par les outils).
import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, Lock, Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { ajouterJours, heure, isoJour, jourCourt, nomJour } from "@/lib/dates";
import { preremplirDock } from "@/lib/dock";
import { couleurAgenda, nomAgenda } from "@/lib/session";
import type { Carte } from "@/lib/types";
import { TitrePage } from "./commun";

type Jour = { date: string; evenements: { titre?: string; debut?: string; lieu?: string; agenda?: string; journee_entiere?: boolean; prive?: boolean }[]; total: number; meteo?: string };
const MOIS_LONGS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];

/** « Semaine du 8 au 14 novembre » (maquette), « du 28 novembre au 4 décembre » à cheval sur deux mois. */
export function titreSemaine(debut: Date, fin: Date): string {
  if (debut.getMonth() === fin.getMonth()) return `Semaine du ${debut.getDate()} au ${fin.getDate()} ${MOIS_LONGS[fin.getMonth()]}`;
  return `Semaine du ${debut.getDate()} ${MOIS_LONGS[debut.getMonth()]} au ${fin.getDate()} ${MOIS_LONGS[fin.getMonth()]}`;
}

/** Numéro de semaine ISO 8601. */
export function numeroSemaine(d: Date): number {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const jour = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - jour);
  const debutAnnee = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t.getTime() - debutAnnee.getTime()) / 864e5 + 1) / 7);
}

export function EcranSemaine() {
  const [decalage, setDecalage] = useState(0);
  const [jours, setJours] = useState<(Jour | null)[]>(Array(7).fill(null));
  const debut = ajouterJours(new Date(), decalage);
  const auj = isoJour(new Date());

  useEffect(() => {
    let actif = true;
    setJours(Array(7).fill(null));
    for (let i = 0; i < 7; i++) {
      const d = isoJour(ajouterJours(new Date(), decalage + i));
      api<{ cartes: Carte[] }>("GET", `/api/brief/jour?date=${d}`)
        .then((r) => {
          const b = r.cartes.find((c) => c.type === "bonjour");
          if (!actif) return;
          setJours((avant) => {
            const n = [...avant];
            n[i] = { date: d, evenements: b?.donnees.evenements ?? [], total: Number(b?.donnees.evenements_total ?? 0) };
            return n;
          });
        })
        .catch(() => {
          if (actif) setJours((avant) => Object.assign([...avant], { [i]: { date: d, evenements: [], total: -1 } }));
        });
    }
    return () => {
      actif = false;
    };
  }, [decalage]);

  const fin = ajouterJours(debut, 6);
  return (
    <>
      <TitrePage
        titre={titreSemaine(debut, fin)}
        sous={`Semaine ${numeroSemaine(debut)} · agenda Famille et le tien ; les événements privés de chacun restent privés.`}
        droite={
          <>
            <Button variant="outline" size="icon" aria-label="Jours précédents" disabled={decalage === 0} onClick={() => setDecalage(decalage - 7)}>
              <ChevronLeft />
            </Button>
            <Button variant="outline" size="icon" aria-label="Jours suivants" disabled={decalage >= 7} onClick={() => setDecalage(decalage + 7)}>
              <ChevronRight />
            </Button>
            <Button variant="default" onClick={() => preremplirDock("Ajoute à l’agenda : ")}>
              <Plus />
              Ajouter
            </Button>
          </>
        }
      />
      <div className="cq">
      <div className="semaine">
        {jours.map((j, i) => {
          const date = ajouterJours(debut, i);
          const iso = isoJour(date);
          const evs = j?.evenements ?? [];
          return (
            <section className={`jour${iso === auj ? " auj" : ""}`} key={iso} aria-label={jourCourt(date)}>
              <div className="jour-t">
                <span>{nomJour(date).replace(/^./, (c) => c.toUpperCase())}</span>
                <span className="n">{date.getDate()}</span>
                {iso === auj && <span className="auj">Aujourd’hui</span>}
              </div>
              <div className="evs">
                {j === null && <div className="jour-vide">…</div>}
                {j && j.total < 0 && <div className="jour-vide">Indisponible</div>}
                {j && j.total >= 0 && !evs.length && <div className="jour-vide">Rien de prévu</div>}
                {evs.map((e, k) => (
                  <div className={`ev${e.prive ? " prive" : ""}`} key={k} style={{ ["--ev" as string]: couleurAgenda(e.agenda) }}>
                    <span className="t">{e.journee_entiere ? "Journée" : heure(e.debut)}</span>
                    <span className="n">
                      {e.prive && <Lock className="mr-1 inline size-3.5 align-[-2px] text-muted-foreground" aria-label="privé" />}
                      {e.titre}
                    </span>
                    {(e.lieu || e.agenda) && <span className="w">{[nomAgenda(e.agenda), e.lieu].filter(Boolean).join(" · ")}</span>}
                  </div>
                ))}
                {j && j.total > j.evenements.length && (
                  <div className="jour-vide">+ {j.total - j.evenements.length} autre{j.total - j.evenements.length > 1 ? "s" : ""}</div>
                )}
              </div>
            </section>
          );
        })}
      </div>
      </div>
    </>
  );
}

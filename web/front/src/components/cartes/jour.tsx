// Cartes du temps : bonjour (carte du jour feuilletable), brief, À noter, agenda d'un jour, événement, échéance,
// créneaux, météo.
import { useState } from "react";
import { ChevronLeft, ChevronRight, Cloud, CloudFog, CloudLightning, CloudRain, CloudSnow, CloudSun, Copy, Lock, MapPin, Sun } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ajouterJours, dateLocale, heure, isoJour, jourCourt, jourRelatif } from "@/lib/dates";
import { messageErreur } from "@/lib/api";
import { naviguer } from "@/lib/routeur";
import { couleurAgenda, nomAgenda } from "@/lib/session";
import { TexteRiche } from "@/lib/texte";
import { toast } from "@/lib/toasts";
import type { Carte } from "@/lib/types";
import { derniereCarte, magasin, useConversation } from "@/store/conversation";
import { BoutonEcouter, BoutonsCarte, texteDeCarte, useContexteCarte } from "./actions";

type Ev = { titre?: string; debut?: string; fin?: string; lieu?: string; agenda?: string; journee_entiere?: boolean; prive?: boolean };

function meteoTexte(m: Record<string, unknown> | null | undefined): string {
  if (!m) return "";
  const max = typeof m.max === "number" ? Math.round(m.max) : null;
  const min = typeof m.min === "number" ? Math.round(m.min) : null;
  const libelle = String(m.libelle || "").toLowerCase();
  const lieu = m.commune ? ` à ${m.commune}` : "";
  const temp = max !== null ? `${max}°` : "";
  const pluie = typeof m.pluie_pct === "number" && m.pluie_pct >= 40 ? ` · pluie ${m.pluie_pct} %` : "";
  return `${temp}${libelle ? `${temp ? " et " : ""}${libelle}` : ""}${lieu}${min !== null && max !== null ? ` · ${min}° au plus bas` : ""}${pluie}`.trim();
}

export function CarteBonjour({ carte }: { carte: Carte }) {
  const { cleMessage } = useContexteCarte();
  const d = carte.donnees;
  const messages = useConversation((e) => e.messages);
  const [charge, setCharge] = useState(false);
  const jour = dateLocale(String(d.date || "")) ?? new Date();
  const decalage = Number(d.decalage_jours ?? 0);
  const evenements: Ev[] = Array.isArray(d.evenements) ? d.evenements : [];
  const total = Number(d.evenements_total ?? evenements.length);
  const courses = derniereCarte(messages, ["courses"]);
  const titre = decalage === 0 ? `Bonjour ${d.prenom}` : carte.titre || jourCourt(jour);
  const changer = async (n: number) => {
    const cible = isoJour(ajouterJours(jour, n));
    setCharge(true);
    try {
      if (cleMessage) await magasin.changerJourDansMessage(cleMessage, carte.id, cible);
      else await magasin.chargerAccueil(cible);
    } catch (e) {
      toast(messageErreur(e));
    } finally {
      setCharge(false);
    }
  };
  const sousTitre = [jourCourt(jour), meteoTexte(d.meteo)].filter(Boolean).join(" · ");
  return (
    <section className="carte jour" data-carte="bonjour" aria-label={titre}>
      <div className="flex items-start justify-between gap-2.5 min-w-0">
        <div className="min-w-0">
          <h1 className="titre-jour">{titre}</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">{sousTitre}</p>
        </div>
        <div className="flex flex-none flex-col items-end gap-2">
          <div className="flex gap-0.5">
            <Button variant="ghost" size="icon-sm" aria-label="Jour précédent" disabled={decalage <= -1 || charge} onClick={() => changer(-1)}>
              <ChevronLeft />
            </Button>
            <Button variant="ghost" size="icon-sm" aria-label="Jour suivant" disabled={decalage >= 14 || charge} onClick={() => changer(1)}>
              <ChevronRight />
            </Button>
          </div>
          {decalage === 0 && <BoutonEcouter texte={texteDeCarte({ ...carte, titre: `${titre}. ${sousTitre}` })} cle={carte.id} />}
        </div>
      </div>
      {charge ? (
        <div className="flex flex-col gap-2.5 py-2" role="status">
          <div className="analyse"><span /></div>
          <span className="text-sm text-muted-foreground">Je regarde la météo et l’agenda du {jourCourt(ajouterJours(jour, 0))}…</span>
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-1.5">
            {evenements.length === 0 && <p className="text-sm text-muted-foreground">Rien à l’agenda.</p>}
            {evenements.slice(0, 4).map((e, i) => (
              <div className="tuile-ev" key={i}>
                <span className="heure">{e.journee_entiere ? "Journée" : heure(e.debut)}</span>
                <span className="point self-center" style={{ ["--c" as string]: couleurAgenda(e.agenda) }} />
                <span className="ellip">
                  {e.titre}
                  {e.prive ? " · privé" : ""}
                </span>
              </div>
            ))}
            {total > 4 && (
              <button className="pl-[50px] text-left text-xs text-muted-foreground hover:underline cursor-pointer" onClick={() => naviguer("semaine")}>
                + {total - 4} autre{total - 4 > 1 ? "s" : ""}
              </button>
            )}
          </div>
          {decalage === 0 && courses && (
            <div className="liens-rapides">
              <button className="cursor-pointer hover:underline" onClick={() => naviguer("courses")}>
                <span className="l">Courses ·</span> {courses.donnees.restants ?? 0} article{Number(courses.donnees.restants) > 1 ? "s" : ""}
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

export function CarteBrief({ carte }: { carte: Carte }) {
  const texte = String(carte.donnees.texte || "");
  return (
    <section className="carte" data-carte="brief">
      <div className="flex items-start justify-between gap-3">
        <h2 className="carte-titre">{carte.titre || "Brief du jour"}</h2>
        <BoutonEcouter texte={String(carte.donnees.texte_oral || texte)} cle={carte.id} className="-mt-1.5 -mr-1.5" />
      </div>
      <div className="text-base leading-relaxed [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:mt-1 [&_p+p]:mt-2 text-pretty">
        <TexteRiche texte={texte} />
      </div>
      <BoutonsCarte carte={carte} exclure={["ecouter"]} />
    </section>
  );
}

type Element = { type?: string; titre?: string; date?: string; dans_jours?: number; en_retard?: boolean; notes?: string; age?: number; a_faire?: boolean; raison?: string };

export function CarteANoter({ carte }: { carte: Carte }) {
  const [plus, setPlus] = useState(false);
  const elements: Element[] = Array.isArray(carte.donnees.elements) ? carte.donnees.elements : [];
  const auj = Number(carte.donnees.aujourdhui ?? 0);
  const semaine = Number(carte.donnees.semaine ?? 0);
  const proches = elements.filter((e) => (e.dans_jours ?? 0) <= 7);
  const tard = elements.filter((e) => (e.dans_jours ?? 0) > 7);
  const visibles = plus ? tard : tard.slice(0, 2);
  const ligne = (e: Element, i: number) => {
    const sous = e.raison || e.notes || (e.type === "anniversaire" && e.age ? `${e.age} ans` : "");
    const aFaire = e.a_faire ?? (e.type !== "anniversaire" && (e.dans_jours ?? 99) <= 7 && !e.en_retard);
    return (
      <div className="note-ligne" key={i}>
        <span className="note-date">{e.date ? jourRelatif(e.date) : ""}</span>
        <span className="min-w-0 flex-1">
          <span className="block ligne-t">{e.titre}</span>
          {sous && <span className="block ligne-s">{sous}</span>}
        </span>
        {e.en_retard ? <Badge variant="danger">En retard</Badge> : aFaire ? <span className="a-faire">À faire</span> : null}
      </div>
    );
  };
  return (
    <section className="carte note" data-carte="a_noter">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
        <h2 className="carte-titre">{carte.titre || "À noter"}</h2>
        <p className="text-sm text-muted-foreground">
          {auj ? `${auj} chose${auj > 1 ? "s" : ""} à faire aujourd’hui` : "Rien d’urgent aujourd’hui"} ·{" "}
          {semaine ? `${semaine} cette semaine` : "rien d’autre cette semaine"}
        </p>
      </div>
      {elements.length === 0 && <p className="text-sm text-muted-foreground">Rien à signaler pour les deux semaines à venir.</p>}
      <div>{proches.map(ligne)}</div>
      {tard.length > 0 && (
        <div>
          <div className="note-sep">Plus tard</div>
          {visibles.map(ligne)}
        </div>
      )}
      {tard.length > 2 && (
        <Button variant="ghost" size="sm" className="-ml-3 self-start text-muted-foreground" aria-expanded={plus} onClick={() => setPlus(!plus)}>
          {plus ? "Afficher moins" : `${tard.length - 2} autre${tard.length - 2 > 1 ? "s" : ""} plus tard`}
        </Button>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function ListeEvenements({ evenements }: { evenements: Ev[] }) {
  if (!evenements.length) return <p className="text-sm text-muted-foreground">Rien à l’agenda.</p>;
  return (
    <div className="evs">
      {evenements.map((e, i) => (
        <div className={`ev${e.prive ? " prive" : ""}`} key={i} style={{ ["--ev" as string]: couleurAgenda(e.agenda) }}>
          <span className="t">{e.journee_entiere ? "Journée" : heure(e.debut)}</span>
          <span className="n">
            {e.prive && <Lock className="mr-1 inline size-3.5 align-[-2px] text-muted-foreground" aria-label="privé" />}
            {e.titre}
          </span>
          {(e.lieu || e.agenda) && <span className="w">{[nomAgenda(e.agenda), e.lieu].filter(Boolean).join(" · ")}</span>}
        </div>
      ))}
    </div>
  );
}

export function CarteAgendaJour({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const debut = dateLocale(String(d.date || ""));
  const fin = d.jusqu_au ? dateLocale(String(d.jusqu_au)) : null;
  const titre = carte.titre || (debut ? (fin ? `Du ${jourCourt(debut)} au ${jourCourt(fin)}` : `Agenda · ${jourCourt(debut)}`) : "Agenda");
  const evenements: Ev[] = Array.isArray(d.evenements) ? d.evenements : [];
  return (
    <section className="carte" data-carte="agenda_jour">
      <h3 className="carte-titre">{titre}</h3>
      {fin ? (
        <div className="flex flex-col gap-3">
          {Object.entries(
            evenements.reduce<Record<string, Ev[]>>((acc, e) => {
              const k = String(e.debut || "").slice(0, 10);
              (acc[k] ||= []).push(e);
              return acc;
            }, {}),
          ).map(([jour, evs]) => (
            <div key={jour}>
              <div className="mb-1.5 text-sm font-semibold">{dateLocale(jour) ? jourCourt(dateLocale(jour)!) : jour}</div>
              <ListeEvenements evenements={evs} />
            </div>
          ))}
          {!evenements.length && <p className="text-sm text-muted-foreground">Rien à l’agenda.</p>}
        </div>
      ) : (
        <ListeEvenements evenements={evenements} />
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

function quand(debut?: string, fin?: string): string {
  const d = dateLocale(String(debut || ""));
  if (!d) return "";
  const h = heure(debut);
  const hf = heure(fin);
  return `${jourCourt(d)}${h ? ` · ${h}` : ""}${hf ? `–${hf}` : ""}`;
}

export function CarteEvenement({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const statut = String(d.statut || "");
  const titre = carte.titre || (statut === "modifie" ? "Événement modifié" : statut === "ajoute" ? "Événement ajouté" : "Proposition d’événement");
  return (
    <section className="carte" data-carte="evenement">
      <div className="carte-titre">{titre}</div>
      <div className="ligne-carte">
        <strong className="font-semibold">
          {d.prive && <Lock className="mr-1 inline size-3.5 align-[-2px]" aria-label="privé" />}
          {d.titre}
        </strong>
        <span className="point mt-1.5" style={{ ["--c" as string]: couleurAgenda(d.agenda) }} />
      </div>
      <div className="ligne-carte">
        <span className="text-muted-foreground">
          {d.ancien_debut && (
            <>
              <s>{heure(d.ancien_debut) || quand(d.ancien_debut)}</s> →{" "}
            </>
          )}
          {quand(d.debut, d.fin)}
        </span>
        {d.agenda && <span className="text-xs text-muted-foreground">Agenda {nomAgenda(d.agenda)}{d.prive ? " · privé" : ""}</span>}
      </div>
      {d.lieu && (
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <MapPin className="size-3.5" />
          {d.lieu}
        </div>
      )}
      {statut === "ajout_manuel" && d.texte_a_copier && (
        <div className="flex items-start gap-2 rounded-md bg-muted p-3 text-sm">
          <span className="min-w-0 flex-1 whitespace-pre-wrap">{String(d.texte_a_copier)}</span>
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label="Copier"
            onClick={() => navigator.clipboard?.writeText(String(d.texte_a_copier)).then(() => toast("Copié."))}
          >
            <Copy />
          </Button>
        </div>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteEcheance({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const date = dateLocale(String(d.prochaine || d.date || ""));
  const rappels: number[] = Array.isArray(d.rappels_jours) ? d.rappels_jours : [];
  return (
    <section className="carte" data-carte="echeance">
      <div className="carte-titre">{carte.titre || "Échéance"}</div>
      <div className="flex items-center gap-3.5">
        {date && (
          <div className="w-10 flex-none leading-tight">
            <div className="text-base font-semibold num">{date.getDate()}</div>
            <div className="text-xs text-muted-foreground">{jourCourt(date).split(" ").pop()}</div>
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="ligne-t">{d.titre}</div>
          <div className="ligne-s">
            {[d.recurrence && `chaque ${d.recurrence}`, rappels.length ? `rappel ${rappels.map((r) => `J-${r}`).join(", ")}` : "", d.notes]
              .filter(Boolean)
              .join(" · ")}
          </div>
        </div>
      </div>
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteCreneaux({ carte }: { carte: Carte }) {
  const creneaux: { libelle?: string; debut?: string; fin?: string; action?: unknown }[] = Array.isArray(carte.donnees.creneaux) ? carte.donnees.creneaux : [];
  return (
    <section className="carte" data-carte="creneaux">
      <div className="carte-titre">{carte.titre || "Créneaux libres"}</div>
      {carte.donnees.precision && <div className="-mt-1 text-xs text-muted-foreground">{String(carte.donnees.precision)}</div>}
      <div className="liste">
        {creneaux.map((c, i) => (
          <div className="flex items-center gap-2.5 py-2 text-sm" key={i}>
            <span className="num flex-1">{c.libelle || quand(c.debut, c.fin)}</span>
            <Button
              size="sm"
              variant={i === 0 ? "default" : "outline"}
              onClick={() => magasin.envoyer(`Je prends le créneau ${c.libelle || quand(c.debut, c.fin)}.`, { contexte: { ecran: "conversation", objet: carte.titre || "créneaux" } })}
            >
              Choisir
            </Button>
          </div>
        ))}
      </div>
      <BoutonsCarte carte={carte} />
    </section>
  );
}

// Carte `meteo` (outil meteo_lire) : commune du foyer, ciel, températures, pluie, vent, matin / après-midi / soir.
const ICONES_METEO: Record<string, LucideIcon> = {
  soleil: Sun,
  "nuage-soleil": CloudSun,
  nuage: Cloud,
  brouillard: CloudFog,
  pluie: CloudRain,
  averse: CloudRain,
  neige: CloudSnow,
  orage: CloudLightning,
};

type Moment = { moment?: string; temperature?: number; pluie_pct?: number; libelle?: string; icone?: string };

const degres = (v: unknown) => (typeof v === "number" ? `${Math.round(v)}°` : "");

export function IconeMeteo({ icone, className = "size-5" }: { icone?: unknown; className?: string }) {
  const Icone = ICONES_METEO[String(icone || "")] ?? Cloud;
  return <Icone className={className} aria-hidden="true" />;
}

export function CarteMeteo({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const moments: Moment[] = Array.isArray(d.moments) ? d.moments : [];
  const actuel = d.actuel && typeof d.actuel === "object" ? (d.actuel as Moment & { heure?: string }) : null;
  const pluie = typeof d.pluie_pct === "number" ? `pluie ${Math.round(d.pluie_pct)} %${typeof d.pluie_mm === "number" && d.pluie_mm > 0 ? ` (${d.pluie_mm} mm)` : ""}` : "";
  const vent = typeof d.vent_kmh === "number" ? `vent ${Math.round(d.vent_kmh)} km/h${typeof d.rafales_kmh === "number" && d.rafales_kmh >= 50 ? `, rafales ${Math.round(d.rafales_kmh)}` : ""}` : "";
  return (
    <section className="carte" data-carte="meteo" aria-label={carte.titre || "Météo"}>
      <div className="carte-titre">{carte.titre || `Météo à ${d.commune}`}</div>
      <div className="flex items-center gap-3.5">
        <IconeMeteo icone={d.icone} className="size-9 flex-none text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="ligne-t">
            <span className="num text-lg font-semibold">{degres(d.max)}</span>
            {d.libelle ? ` · ${d.libelle}` : ""}
          </div>
          <div className="ligne-s">{[typeof d.min === "number" ? `${degres(d.min)} au plus bas` : "", pluie, vent].filter(Boolean).join(" · ")}</div>
        </div>
      </div>
      {actuel && typeof actuel.temperature === "number" && (
        <p className="text-sm text-muted-foreground">
          En ce moment : <span className="num">{degres(actuel.temperature)}</span>
          {actuel.libelle ? `, ${String(actuel.libelle).toLowerCase()}` : ""}
        </p>
      )}
      {moments.length > 0 && (
        <div className="grid grid-cols-3 gap-2" role="list">
          {moments.map((m, i) => (
            <div className="flex min-w-0 flex-col items-center gap-1 rounded-lg bg-muted/60 px-2 py-2 text-center" role="listitem" key={i} title={m.libelle}>
              <span className="text-xs text-muted-foreground">{m.moment}</span>
              <IconeMeteo icone={m.icone} />
              <span className="num text-sm font-medium">{degres(m.temperature)}</span>
              {typeof m.pluie_pct === "number" && m.pluie_pct >= 20 && <span className="num text-xs text-muted-foreground">{Math.round(m.pluie_pct)} %</span>}
            </div>
          ))}
        </div>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

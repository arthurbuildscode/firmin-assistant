// Cartes de la maison : courses, repas de la semaine, propositions Kitchen Daily (compactes au fil, riches au geste),
// extras, menu pour des invités.
import { useState } from "react";
import { ChefHat } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { dateLocale, jourCourt } from "@/lib/dates";
import { naviguer } from "@/lib/routeur";
import type { Carte } from "@/lib/types";
import { useConversation } from "@/store/conversation";
import { BoutonsCarte, useContexteCarte } from "./actions";

type Article = { id?: number; article?: string; coche?: boolean; ajoute_par?: string };
type Rayon = { rayon?: string; articles?: Article[] };

export function CarteCourses({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const rayons: Rayon[] = Array.isArray(d.rayons) ? d.rayons : [];
  const ajoutes = new Set<string>((d.ajoutes || []).map((x: string) => String(x).toLowerCase()));
  const nonVides = rayons
    .map((r) => ({ nom: r.rayon || "Autre", articles: (r.articles || []).filter((a) => !a.coche) }))
    .filter((r) => r.articles.length);
  const montres = nonVides.slice(0, 3);
  const reste = nonVides.slice(3).reduce((n, r) => n + r.articles.length, 0);
  const restants = Number(d.restants ?? nonVides.reduce((n, r) => n + r.articles.length, 0));
  return (
    <section className="carte" data-carte="courses">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="carte-titre">{carte.titre || "Liste de courses"}</h3>
        <span className="text-sm text-muted-foreground num">
          {restants} article{restants > 1 ? "s" : ""}
        </span>
      </div>
      {montres.map((r) => (
        <p className="text-sm" key={r.nom}>
          <span className="text-muted-foreground">{r.nom} · </span>
          {r.articles.map((a, i) => (
            <span key={i}>
              {i > 0 && ", "}
              {ajoutes.has(String(a.article || "").toLowerCase()) ? (
                <>
                  <b className="font-semibold">{a.article}</b> <span className="text-muted-foreground">(nouveau)</span>
                </>
              ) : (
                a.article
              )}
            </span>
          ))}
        </p>
      ))}
      {!montres.length && <p className="text-sm text-muted-foreground">Tout est coché.</p>}
      {reste > 0 && (
        <div className="text-xs text-muted-foreground">
          + {reste} article{reste > 1 ? "s" : ""} dans les autres rayons
        </div>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

const SOURCES: Record<string, string> = {
  maison: "Maison",
  kitchen_daily: "Kitchen Daily",
  kd: "Kitchen Daily",
  invites: "Invités",
  restaurant: "Restau",
  restau: "Restau",
};

type Repas = { titre?: string; nom?: string; source?: string; note?: string; rituel?: boolean } | string | null | undefined;
const repasNom = (r: Repas) => (typeof r === "string" ? r : r?.titre || r?.nom || "");
const repasSource = (r: Repas) => (typeof r === "string" ? "" : SOURCES[String(r?.source || "")] || r?.source || "");

export function PlanningRepas({ jours, aujourdhui }: { jours: { date?: string; midi?: Repas; soir?: Repas }[]; aujourdhui?: string }) {
  return (
    <div className="cq">
    <div className="planning">
      {jours.map((j, i) => {
        const d = dateLocale(String(j.date || ""));
        return (
          <section className={`pjour${j.date === aujourdhui ? " auj" : ""}`} key={i} aria-label={d ? jourCourt(d) : undefined}>
            <div className="pjour-t">
              <span>{d ? jourCourt(d).split(" ")[0] : ""}</span>
              <span className="n">{d?.getDate()}</span>
            </div>
            <div className="repas-2">
              {(["midi", "soir"] as const).map((creneau) => {
                const r = j[creneau];
                const nom = repasNom(r);
                return (
                  <div className="repas-1" key={creneau}>
                    <span className="creneau">{creneau === "midi" ? "Midi" : "Soir"}</span>
                    <span className={`nom${typeof r === "object" && r?.rituel ? " font-semibold" : ""}`}>{nom || "—"}</span>
                    {repasSource(r) && <span className="src">{repasSource(r)}</span>}
                    {typeof r === "object" && r?.note && <span className="src">{r.note}</span>}
                  </div>
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
    </div>
  );
}

export function CarteRepasSemaine({ carte }: { carte: Carte }) {
  const { ecran } = useContexteCarte();
  const jours = Array.isArray(carte.donnees.jours) ? carte.donnees.jours : [];
  return (
    <section className={`carte${ecran ? " large" : ""}`} data-carte="repas_semaine">
      <h3 className="carte-titre">{carte.titre || "Repas de la semaine"}</h3>
      <PlanningRepas jours={ecran ? jours : jours.slice(0, 3)} />
      {!ecran && jours.length > 3 && (
        <Button variant="ghost" size="sm" className="-ml-3 self-start" onClick={() => naviguer("repas")}>
          Voir le planning
        </Button>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

type Plat = { titre?: string; quantite?: number; image?: string; badges?: string[]; ingredients?: string[] | string; description?: string };

function ImagePlat({ plat }: { plat: Plat }) {
  const [erreur, setErreur] = useState(false);
  const url = typeof plat.image === "string" && /^https:\/\/|^\//.test(plat.image) ? plat.image : "";
  return (
    <div className="plat-image">
      {url && !erreur ? <img src={url} alt="" loading="lazy" referrerPolicy="no-referrer" onError={() => setErreur(true)} /> : <ChefHat className="size-7" />}
      {(plat.quantite ?? 0) > 0 && <span className="plat-q num">×{plat.quantite}</span>}
    </div>
  );
}

export function ListePlats({ plats }: { plats: Plat[] }) {
  return (
    <div className="cq">
    <div className="plats">
      {plats.map((p, i) => (
        <div className={`plat${p.quantite === 0 ? " opacity-55" : ""}`} key={i}>
          <ImagePlat plat={p} />
          <div className="plat-b min-w-0">
            <div className="plat-n">{p.titre}</div>
            {(p.ingredients || p.description) && (
              <div className="plat-ing">{Array.isArray(p.ingredients) ? p.ingredients.join(" · ") : p.ingredients || p.description}</div>
            )}
            {!!p.badges?.length && (
              <div className="plat-badges">
                {p.badges.map((b) => (
                  <Badge key={b} className="h-5 px-1.5">
                    {b}
                  </Badge>
                ))}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
    </div>
  );
}

export function CarteKitchenDaily({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const plats: Plat[] = Array.isArray(d.plats) ? d.plats : [];
  const decisions = useConversation((e) => e.decisions);
  const aValider = carte.actions.some((a) => typeof a.confirmation === "number" && !decisions[a.confirmation]);
  // Compacte si la carte le demande (accueil, écran Repas) : une phrase d'état, le détail s'ouvre au geste.
  const [ouvert, setOuvert] = useState(!d.compact);
  const total = plats.reduce((n, p) => n + (p.quantite ?? 0), 0);
  const pied = [...new Set([d.total_plats ?? (total || null) ? `${d.total_plats ?? total} plats` : "", d.total, d.formule].filter(Boolean))].join(" · ");
  return (
    <section className={`carte${ouvert ? " large" : ""}`} data-carte="kitchen_daily_proposition">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="carte-titre">{carte.titre || "Proposition Kitchen Daily"}</h3>
        {d.limite && <Badge>{String(d.limite)}</Badge>}
      </div>
      {ouvert ? (
        <ListePlats plats={plats} />
      ) : (
        <p className="text-sm text-muted-foreground">
          {plats.length} plat{plats.length > 1 ? "s" : ""}
          {d.resume ? ` · ${d.resume}` : ""}
        </p>
      )}
      {ouvert && pied && <div className="border-t pt-2.5 text-sm text-muted-foreground">{pied}</div>}
      <div className="boutons items-center">
        {!ouvert && (
          <Button size="sm" variant={aValider ? "default" : "secondary"} onClick={() => setOuvert(true)}>
            {aValider ? `Voir les ${plats.length} plats et valider` : "Voir la proposition"}
          </Button>
        )}
        {ouvert && <BoutonsCarte carte={carte} />}
      </div>
    </section>
  );
}

export function CarteKitchenDailyExtras({ carte }: { carte: Carte }) {
  const categories: { nom?: string; categorie?: string; articles?: { titre?: string; prix?: string | number; quantite?: number }[] }[] = Array.isArray(carte.donnees.categories)
    ? carte.donnees.categories
    : [];
  return (
    <section className="carte" data-carte="kitchen_daily_extras">
      <h3 className="carte-titre">{carte.titre || "Extras de la livraison"}</h3>
      {categories.map((c, i) => (
        <div key={i}>
          <div className="mb-1 text-sm font-semibold text-muted-foreground">{c.nom || c.categorie}</div>
          <div className="liste">
            {(c.articles || []).map((a, j) => (
              <div className="ligne-carte py-1.5" key={j}>
                <span>
                  {a.titre}
                  {a.quantite ? ` ×${a.quantite}` : ""}
                </span>
                {a.prix !== undefined && <span className="num text-muted-foreground">{typeof a.prix === "number" ? `${a.prix.toFixed(2).replace(".", ",")} €` : a.prix}</span>}
              </div>
            ))}
          </div>
        </div>
      ))}
      {carte.donnees.supplement && <div className="text-sm">Supplément : {String(carte.donnees.supplement)}</div>}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteMenuInvites({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const plats: { moment?: string; titre?: string }[] = Array.isArray(d.plats) ? d.plats : [];
  const courses: string[] = Array.isArray(d.courses) ? d.courses : [];
  return (
    <section className="carte" data-carte="menu_invites">
      <div className="carte-titre">{carte.titre || "Menu pour les invités"}</div>
      {plats.map((p, i) => (
        <div className="ligne-carte" key={i}>
          <span className="text-muted-foreground">{p.moment}</span>
          <span className="text-right font-medium">{p.titre}</span>
        </div>
      ))}
      {d.adaptations && <div className="text-xs text-muted-foreground">{String(d.adaptations)}</div>}
      {courses.length > 0 && (
        <p className="text-sm">
          <span className="text-muted-foreground">Courses à ajouter ({courses.length}) · </span>
          {courses.join(", ")}
        </p>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

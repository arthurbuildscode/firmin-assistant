// Courses : la liste partagée, en plein écran (une colonne, par rayon). Source : le point d'accès direct
// `GET /api/courses` (cocher sur place : `PATCH /api/courses/{id}` ; ajouter : `POST /api/courses`), sinon la dernière
// carte « courses » du fil. Cocher ne réordonne jamais rien : l'article change d'état sur place ; le rangement
// (retirer les cochés) est une action explicite et séparée.
import { useMemo, useState } from "react";
import { Plus, Store } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, messageErreur } from "@/lib/api";
import { jourRelatif } from "@/lib/dates";
import { libelleRayon, rayonsDe, useDonnees, type ArticleCourses, type RayonCourses } from "@/lib/donnees";
import { toast } from "@/lib/toasts";
import { derniereCarte, magasin, useConversation } from "@/store/conversation";
import { BoutonActualiser, LienBas, TitrePage, Vide } from "./commun";

const valider = (brut: unknown) => {
  const rayons = rayonsDe(brut);
  return rayons ? { rayons } : null;
};

/** « ce matin », « aujourd’hui », « hier », « jeu. » : quand l'article a été ajouté (maquette). */
function quand(ts?: string): string {
  if (!ts) return "";
  const t = jourRelatif(ts.slice(0, 10));
  if (t !== "aujourd’hui") return t;
  const h = new Date(ts).getHours();
  return !Number.isNaN(h) && h < 12 ? "ce matin" : "aujourd’hui";
}

function remplacerArticle(rayons: RayonCourses[], article: ArticleCourses): RayonCourses[] {
  return rayons.map((r) => ({ ...r, articles: (r.articles || []).map((a) => (a.id === article.id ? { ...a, ...article } : a)) }));
}

export function EcranCourses() {
  const messages = useConversation((e) => e.messages);
  const enCours = useConversation((e) => e.echangeEnCours);
  const direct = useDonnees("courses", valider);
  const carte = derniereCarte(messages, ["courses"]);
  const [modifies, setModifies] = useState<Record<string, boolean>>({});
  const [enMagasin, setEnMagasin] = useState(false);
  const [nouveau, setNouveau] = useState("");
  const contexte = { ecran: "courses", objet: "liste de courses" };
  const enDirect = direct.statut === "ok";

  const source: RayonCourses[] | null = enDirect ? direct.donnees!.rayons : carte ? ((carte.donnees.rayons as RayonCourses[]) ?? []) : null;
  const rayons = useMemo(
    () => (source || []).map((r) => ({ nom: libelleRayon(r.rayon), articles: r.articles || [] })).filter((r) => r.articles.length),
    [source],
  );
  const cle = (a: ArticleCourses) => String(a.id ?? a.article);
  const coche = (a: ArticleCourses) => (cle(a) in modifies ? modifies[cle(a)] : !!a.coche);
  const tous = rayons.flatMap((r) => r.articles);
  const restants = tous.filter((a) => !coche(a)).length;
  // Repli (sans point d'accès) : les changements partent ensemble, par la conversation.
  const aEnregistrer = enDirect ? [] : tous.filter((a) => cle(a) in modifies && modifies[cle(a)] !== !!a.coche);

  const basculer = async (a: ArticleCourses, valeur: boolean) => {
    setModifies((m) => ({ ...m, [cle(a)]: valeur }));
    if (!enDirect || a.id === undefined) return;
    try {
      const r = await api<{ article?: ArticleCourses }>("PATCH", `/api/courses/${encodeURIComponent(String(a.id))}`, { coche: valeur });
      // Réponse : l'article à jour (`{article}`), ou la liste entière ; la liste ne se réordonne jamais.
      const v = valider(r) ?? (r?.article && direct.donnees ? { rayons: remplacerArticle(direct.donnees.rayons, r.article) } : null);
      if (v) {
        direct.remplacer(v);
        setModifies((m) => {
          const n = { ...m };
          delete n[cle(a)];
          return n;
        });
      }
    } catch (e) {
      setModifies((m) => ({ ...m, [cle(a)]: !valeur }));
      toast(messageErreur(e));
    }
  };

  const enregistrer = async () => {
    const coches = aEnregistrer.filter((a) => coche(a)).map((a) => a.article);
    const decoches = aEnregistrer.filter((a) => !coche(a)).map((a) => a.article);
    const morceaux = [coches.length ? `Coche dans la liste de courses : ${coches.join(", ")}.` : "", decoches.length ? `Décoche : ${decoches.join(", ")}.` : ""];
    const r = await magasin.envoyer(morceaux.filter(Boolean).join(" "), { contexte });
    if (r && !r.erreur) setModifies({});
  };

  const ajouter = async (e: React.FormEvent) => {
    e.preventDefault();
    const t = nouveau.trim();
    if (!t) return;
    setNouveau("");
    if (enDirect) {
      try {
        const r = await api<unknown>("POST", "/api/courses", { articles: [t] });
        const v = valider(r);
        if (v) direct.remplacer(v);
        else void direct.recharger();
        return;
      } catch (err) {
        toast(messageErreur(err));
        return;
      }
    }
    void magasin.envoyer(`Ajoute aux courses : ${t}`, { contexte });
  };

  const viderCoches = async () => {
    if (!enDirect) {
      void magasin.envoyer("Retire de la liste de courses les articles cochés.", { contexte });
      return;
    }
    try {
      const r = await api<unknown>("DELETE", "/api/courses/coches");
      const v = valider(r);
      setModifies({});
      if (v) direct.remplacer(v);
      else void direct.recharger();
    } catch (e) {
      toast(messageErreur(e));
    }
  };

  const cochesN = tous.length - restants;
  return (
    <div className={enMagasin ? "magasin" : ""}>
      <TitrePage
        titre="Courses"
        sous={source ? `${restants} à acheter · ${cochesN} coché${cochesN > 1 ? "s" : ""} · liste partagée` : undefined}
      />
      {!source ? (
        direct.statut === "chargement" ? null : (
          <Vide action={<BoutonActualiser texte="Montre-moi la liste de courses." contexte={contexte} libelle="Afficher la liste" />}>
            La liste de courses s’affiche ici dès que tu la demandes.
          </Vide>
        )
      ) : (
        <>
          <div className="mb-4 flex flex-wrap gap-2">
            <Button variant={enMagasin ? "default" : "outline"} onClick={() => setEnMagasin(!enMagasin)} aria-pressed={enMagasin}>
              {enMagasin && <Store />}
              {enMagasin ? "Quitter le mode magasin" : "Mode en magasin"}
            </Button>
            <Button variant="ghost" disabled={(!enDirect && enCours) || !cochesN} onClick={() => void viderCoches()}>
              Vider les cochés
            </Button>
          </div>
          {!enMagasin && (
            <form className="mb-6 flex max-w-[720px] gap-2" onSubmit={ajouter}>
              <input className="champ min-w-0 flex-1" value={nouveau} onChange={(e) => setNouveau(e.target.value)} placeholder="Ajouter un article, ex. « 6 œufs »" aria-label="Article à ajouter" />
              <Button type="submit" variant="default" className="h-10">
                <Plus />
                Ajouter
              </Button>
            </form>
          )}
          {rayons.length === 0 && <Vide>La liste est vide.</Vide>}
          <div className="rayons">
            {rayons.map((r) => (
              <section key={r.nom} aria-label={r.nom}>
                <div className="mb-0.5 flex items-center justify-between">
                  <h2 className="rayon-t">{r.nom}</h2>
                  <span className="num text-sm text-muted-foreground">
                    {r.articles.filter((a) => !coche(a)).length}/{r.articles.length}
                  </span>
                </div>
                <div>
                  {r.articles.map((a) => {
                    const qui = [a.ajoute_par, quand(a.ts)].filter(Boolean).join(" · ");
                    return (
                      <label className={`article${coche(a) ? " coche" : ""}`} key={cle(a)}>
                        <input type="checkbox" className="case" checked={coche(a)} onChange={(e) => void basculer(a, e.target.checked)} />
                        <span className="min-w-0 flex-1">
                          <span className="nm block">
                            {a.article}
                            {a.quantite ? ` ${a.quantite}` : ""}
                          </span>
                          {qui && <span className="qui block">{qui}</span>}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
          {aEnregistrer.length > 0 && (
            <div className="sticky bottom-[calc(var(--tabbar-h)+var(--dock-h)+12px)] z-10 mt-6 flex max-w-[720px] items-center justify-between gap-3 rounded-lg border bg-popover p-3 shadow-[var(--shadow-sm)] lg:bottom-[calc(var(--dock-h)+12px)]">
              <span className="text-sm">
                {aEnregistrer.length} changement{aEnregistrer.length > 1 ? "s" : ""} à enregistrer
              </span>
              <div className="flex gap-2">
                <Button size="sm" variant="ghost" onClick={() => setModifies({})}>
                  Annuler
                </Button>
                <Button size="sm" variant="default" disabled={enCours} onClick={enregistrer}>
                  Enregistrer
                </Button>
              </div>
            </div>
          )}
          <div className="mt-8">
            <LienBas ecran="routines">Routines courses</LienBas>
          </div>
        </>
      )}
    </div>
  );
}

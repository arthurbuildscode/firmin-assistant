// Autres cartes du contrat : comparatif, documents, bus, routines, réflexion, compte rendu, enregistrement,
// capacité proposée, journal, suggestions, capture, et un rendu générique sobre pour le reste.
import { useState } from "react";
import { AudioLines, Bus, FileText, Fingerprint, Lock, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { messageErreur } from "@/lib/api";
import { duree, heureDe } from "@/lib/dates";
import { ecranCourant } from "@/lib/routeur";
import { TexteRiche } from "@/lib/texte";
import { toast } from "@/lib/toasts";
import type { Carte } from "@/lib/types";
import { magasin } from "@/store/conversation";
import { BoutonEcouter, BoutonsCarte, executerAction } from "./actions";

export function CarteComparatif({ carte }: { carte: Carte }) {
  const offres: { nom?: string; prix?: string | number; detail?: string; recommande?: boolean }[] = Array.isArray(carte.donnees.offres) ? carte.donnees.offres : [];
  return (
    <section className="carte" data-carte="comparatif">
      <h3 className="carte-titre">{carte.titre || "Comparatif"}</h3>
      <div>
        {offres.map((o, i) => (
          <div className={`offre${o.recommande ? " recommandee" : ""}`} key={i}>
            <span className="n">
              {o.nom} {o.recommande && <Badge variant="soft">Recommandé</Badge>}
            </span>
            <span className="p">{typeof o.prix === "number" ? `${o.prix.toFixed(2).replace(".", ",")} €` : o.prix}</span>
            {o.detail && <span className="d">{o.detail}</span>}
          </div>
        ))}
      </div>
      {carte.donnees.gain && <p className="text-sm">{String(carte.donnees.gain)}</p>}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteFichierClasse({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  return (
    <section className="carte" data-carte="fichier_classe">
      <div className="flex items-start gap-3">
        <FileText className="mt-0.5 size-5 flex-none text-muted-foreground" />
        <div className="min-w-0">
          <div className="ligne-t">{d.description || d.nom}</div>
          <div className="ligne-s">
            {carte.titre || "Rangé"} dans {String(d.categorie)}
          </div>
        </div>
      </div>
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteFichierRecu({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const url = typeof d.url === "string" && d.url.startsWith("/") ? d.url : "";
  return (
    <section className="carte w-auto max-w-[360px] gap-1.5 p-2" data-carte="fichier_recu">
      {url ? (
        <img src={url} alt="" className="h-24 w-[132px] rounded-[10px] object-cover" />
      ) : (
        <div className="flex items-center gap-2.5 px-1.5 py-1">
          <FileText className="size-[22px]" />
          <div>
            <div className="text-sm font-semibold">{d.nom}</div>
            {d.detail && <div className="text-xs text-muted-foreground">{String(d.detail)}</div>}
          </div>
        </div>
      )}
      {url && <span className="text-xs">{d.nom}</span>}
    </section>
  );
}

export function VignetteVerrouillee({ nom, detail, onDeverrouiller }: { nom: string; detail?: string; onDeverrouiller?: () => void }) {
  return (
    <figure className="doc-v">
      <button className="img cursor-pointer" aria-label={`Déverrouiller ${nom}`} onClick={onDeverrouiller}>
        <div className="lignes" aria-hidden>
          <i style={{ width: "70%" }} />
          <i style={{ width: "55%" }} />
          <i style={{ width: "62%" }} />
        </div>
        <span className="verrou">
          <span>
            <Lock className="size-4" />
          </span>
        </span>
      </button>
      <figcaption className="text-sm leading-snug">
        <span className="font-semibold">{nom}</span>
        <span className="block text-xs text-muted-foreground">{detail || "Verrouillé"}</span>
      </figcaption>
    </figure>
  );
}

export function CarteDocumentSensible({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const action = carte.actions.find((a) => a.action === "deverrouiller");
  const deverrouiller = async () => {
    try {
      await executerAction(action ?? { libelle: "Déverrouiller", action: "deverrouiller", args: {} }, carte);
    } catch (e) {
      toast(messageErreur(e));
    }
  };
  return (
    <section className="carte" data-carte="document_sensible">
      <div className="w-[min(100%,220px)]">
        <VignetteVerrouillee nom={String(d.nom)} detail={d.acces ? `Verrouillé · ${d.acces}` : undefined} onDeverrouiller={deverrouiller} />
      </div>
      <div className="boutons">
        <Button size="sm" variant="outline" onClick={deverrouiller}>
          <Fingerprint />
          Déverrouiller
        </Button>
      </div>
    </section>
  );
}

export function CarteBus({ carte }: { carte: Carte }) {
  const passages: { heure_theorique?: string; heure_estimee?: string; retard_min?: number; resume?: string; dans_min?: number }[] = Array.isArray(carte.donnees.passages)
    ? carte.donnees.passages
    : [];
  return (
    <section className="carte" data-carte="bus">
      <div className="carte-titre flex items-center gap-2">
        <Bus className="size-4 text-muted-foreground" />
        {carte.titre || [carte.donnees.ligne, carte.donnees.arret].filter(Boolean).join(" · ") || "Prochains bus"}
      </div>
      {passages.slice(0, 5).map((p, i) => (
        <div className="ligne-carte" key={i}>
          <span className="num">{p.heure_estimee || p.heure_theorique}</span>
          <span className={p.retard_min ? "" : "text-muted-foreground"}>
            {p.resume || (p.retard_min ? `+${p.retard_min} min` : "à l’heure")}
            {p.dans_min !== undefined ? ` · dans ${p.dans_min} min` : ""}
          </span>
        </div>
      ))}
      {!passages.length && <p className="text-sm text-muted-foreground">Aucun passage dans l’heure.</p>}
      {carte.donnees.temps_reel === false && <p className="text-xs text-muted-foreground">Horaires théoriques (temps réel indisponible).</p>}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteTexte({ carte, nom }: { carte: Carte; nom: string }) {
  const d = carte.donnees;
  const texte = String(d.texte || "");
  const long = texte.length > 220;
  return (
    <section className="carte" data-carte={nom}>
      <div className="flex items-start justify-between gap-3">
        <div className="carte-titre">{carte.titre || (nom === "compte_rendu" ? "Compte rendu" : "Routine")}</div>
        {long && <BoutonEcouter texte={texte} cle={carte.id} className="-mt-1.5 -mr-1.5" />}
      </div>
      <div className="text-[.9375rem] leading-relaxed [&_ul]:list-disc [&_ul]:pl-5 [&_p+p]:mt-2">
        <TexteRiche texte={texte} />
      </div>
      {d.quand && <div className="text-xs text-muted-foreground">{heureDe(String(d.quand)) || String(d.quand)}</div>}
      <BoutonsCarte carte={carte} exclure={["ecouter"]} />
    </section>
  );
}

export function CarteEnregistrement({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const fini = /analys[ée]|termin|fait/i.test(String(d.statut));
  return (
    <section className="carte max-w-[360px]" data-carte="enregistrement">
      <div className="flex items-center gap-2.5">
        <AudioLines className="size-5 text-muted-foreground" />
        <div className="min-w-0">
          <div className="text-sm font-semibold">
            Enregistrement {d.duree ? `${d.duree} ` : ""}— {fini ? "analysé" : "analyse en cours"}
          </div>
          {d.detail && <div className="text-xs text-muted-foreground">{String(d.detail)}</div>}
        </div>
      </div>
      {!fini && (
        <div className="analyse">
          <span />
        </div>
      )}
    </section>
  );
}

export function CarteCapacite({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const points: string[] = Array.isArray(d.ce_qu_elle_fera) ? d.ce_qu_elle_fera : Array.isArray(d.points) ? d.points : [];
  return (
    <section className="carte" data-carte="capacite_proposee">
      <h2 className="carte-titre flex items-center gap-2">
        <Sparkles className="size-4 text-muted-foreground" />
        {String(d.titre)}
      </h2>
      {d.description && <p className="text-sm">{String(d.description)}</p>}
      {points.length > 0 && (
        <ul className="m-0 flex list-disc flex-col gap-0.5 pl-[18px] text-sm">
          {points.map((p, i) => (
            <li key={i}>{p}</li>
          ))}
        </ul>
      )}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

export function CarteJournal({ carte }: { carte: Carte }) {
  const lignes: { quand?: string; qui?: string; texte?: string }[] = Array.isArray(carte.donnees.lignes) ? carte.donnees.lignes : [];
  const [ouvert, setOuvert] = useState(false);
  return (
    <details className="w-[min(100%,580px)] text-sm" open={ouvert} onToggle={(e) => setOuvert((e.target as HTMLDetailsElement).open)} data-carte="journal">
      <summary className="inline-flex cursor-pointer list-none items-center gap-1.5 py-1 text-muted-foreground hover:text-foreground">
        {carte.titre || "Dernières actions"}
      </summary>
      <div className="liste mt-1">
        {lignes.map((l, i) => (
          <div className="flex items-start gap-3 py-2.5" key={i}>
            <span className="w-11 flex-none pt-0.5 text-xs text-muted-foreground">{heureDe(l.quand) || l.quand}</span>
            <div className="min-w-0 flex-1">
              {l.qui && <b className="font-semibold">{l.qui} </b>}
              {l.qui && <span className="text-muted-foreground">· </span>}
              {l.texte}
            </div>
          </div>
        ))}
      </div>
    </details>
  );
}

export function CarteSuggestions({ carte }: { carte: Carte }) {
  const suggestions: (string | { texte?: string; libelle?: string })[] = Array.isArray(carte.donnees.suggestions) ? carte.donnees.suggestions : [];
  return (
    <div className="flex flex-wrap gap-1.5" data-carte="suggestions">
      {suggestions.map((s, i) => {
        const texte = typeof s === "string" ? s : s.texte || s.libelle || "";
        return (
          <button key={i} className="puce" onClick={() => magasin.envoyer(texte, { contexte: { ecran: ecranCourant() } })}>
            {typeof s === "string" ? s : s.libelle || s.texte}
          </button>
        );
      })}
    </div>
  );
}

export function CarteCapture({ carte }: { carte: Carte }) {
  const url = String(carte.donnees.url || "");
  if (!/^\/api\/captures\/[A-Za-z0-9][A-Za-z0-9._-]*$/.test(url)) return null;
  return (
    <figure className="carte m-0 w-[min(100%,420px)] gap-2 p-2" data-carte="capture">
      <a href={url} target="_blank" rel="noopener">
        <img src={url} alt={String(carte.donnees.legende || "Capture d’écran")} className="w-full rounded-[10px] border" loading="lazy" />
      </a>
      {carte.donnees.legende && <figcaption className="px-1 text-xs text-muted-foreground">{String(carte.donnees.legende)}</figcaption>}
    </figure>
  );
}

export function CarteReflexion({ carte }: { carte: Carte }) {
  const etapes: { etape?: string; libelle?: string; duree_ms?: number }[] = Array.isArray(carte.donnees.etapes) ? carte.donnees.etapes : [];
  return (
    <ol className="etapes" data-carte="reflexion">
      {etapes.map((e, i) => (
        <li key={i}>
          {(e.etape || e.libelle || "").replace("…", "")}
          {e.duree_ms ? <span className="d">{duree(e.duree_ms)}</span> : null}
        </li>
      ))}
    </ol>
  );
}

/** Type connu sans rendu dédié : titre et texte, jamais de JSON brut ni de nom technique. */
export function CarteGenerique({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const texte = [d.texte, d.description, d.nom, d.titre].find((x) => typeof x === "string" && x) as string | undefined;
  return (
    <section className="carte" data-carte={carte.type}>
      {carte.titre && <div className="carte-titre">{carte.titre}</div>}
      {texte && <p className="text-sm">{texte}</p>}
      <BoutonsCarte carte={carte} />
    </section>
  );
}

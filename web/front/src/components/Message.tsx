// Un message du fil : bulle, « Transmis à … » (masquage), ligne de réflexion (étapes dépliables), cartes,
// confirmations.
import { useEffect, useState } from "react";
import { AlertCircle, ChevronRight, Lock, Mic, Paperclip } from "lucide-react";
import { duree, heureDe } from "@/lib/dates";
import { useAssistant } from "@/lib/session";
import { nettoyerFlux, TexteRiche } from "@/lib/texte";
import type { Carte as TCarte, Etape, MessageFil } from "@/lib/types";
import { BarreReponse } from "./BarreReponse";
import { Carte } from "./cartes/Carte";
import { ConfirmationEnLigne } from "./cartes/actions";

const NOMS_ECRANS: Record<string, string> = {
  semaine: "Semaine",
  courses: "Courses",
  repas: "Repas",
  echeances: "Échéances",
  journal: "Journal",
  fichiers: "Fichiers",
  memoire: "Mémoire",
  routines: "Routines",
  reglages: "Réglages",
};

function dureeEtape(e: Etape): number | undefined {
  if (typeof e.duree_ms === "number") return e.duree_ms;
  if (e.debut && e.fin) return e.fin - e.debut;
  return undefined;
}

function ListeEtapes({ etapes, courante }: { etapes: Etape[]; courante?: Etape }) {
  return (
    <ol className="etapes">
      {etapes.map((e, i) => (
        <li key={i}>
          {e.etape.replace("…", "")}
          {dureeEtape(e) !== undefined && <span className="d">{duree(dureeEtape(e))}</span>}
        </li>
      ))}
      {courante && <li className="courante">{courante.etape}</li>}
    </ol>
  );
}

function ReflexionEnCours({ m }: { m: MessageFil }) {
  const [ouvert, setOuvert] = useState(false);
  const [maintenant, setMaintenant] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setMaintenant(Date.now()), 250);
    return () => window.clearInterval(t);
  }, []);
  const courante = m.etapes[m.etapes.length - 1];
  const libelle = m.attente || courante?.etape || "Je réfléchis…";
  const secondes = Math.max(0, Math.floor((maintenant - (m.debutMs ?? maintenant)) / 1000));
  return (
    <>
      <button className="reflexion" aria-expanded={ouvert} onClick={() => setOuvert(!ouvert)} data-reflexion="en-cours">
        <span className="points-attente" aria-hidden>
          <i />
          <i />
          <i />
        </span>
        <span className="pulsation" role="status">
          {libelle}
        </span>
        <span className="num text-xs">{secondes} s</span>
        {m.etapes.length > 0 && <ChevronRight className="chevron size-3.5" />}
      </button>
      {ouvert && m.etapes.length > 0 && <ListeEtapes etapes={m.etapes.slice(0, -1)} courante={courante} />}
    </>
  );
}

function ReflexionFinie({ m }: { m: MessageFil }) {
  const [ouvert, setOuvert] = useState(false);
  if (!m.etapes.length) return null;
  const total = m.dureeMs ?? m.etapes.reduce((s, e) => s + (dureeEtape(e) ?? 0), 0);
  return (
    <>
      <button className="reflexion finie" aria-expanded={ouvert} onClick={() => setOuvert(!ouvert)}>
        Réfléchi {duree(total) || "1 s"} · {m.etapes.length} étape{m.etapes.length > 1 ? "s" : ""}
        <ChevronRight className="chevron size-3.5" />
      </button>
      {ouvert && <ListeEtapes etapes={m.etapes} />}
    </>
  );
}

// Jetons posés par le filtre de données sensibles du cœur (lot 5c) dans le texte stocké.
const RE_MASQUE = /\[(mot de passe masqué|clé masquée|carte bancaire ••\d{2,4}|IBAN ••\w+|numéro de sécurité sociale)\]/;
// Cartes du jour : en tête du message, avant la réponse (le fil s'ouvre sur la journée), dans l'ordre de la maquette :
// bonjour, « À noter », puis le brief rédigé.
export const TYPES_DU_JOUR = new Set(["bonjour", "brief", "a_noter"]);
const ORDRE_DU_JOUR: Record<string, number> = { bonjour: 0, a_noter: 1, brief: 2 };

export function cartesDuJour(m: MessageFil): TCarte[] {
  return m.cartes.filter((c) => TYPES_DU_JOUR.has(c.type)).sort((a, b) => (ORDRE_DU_JOUR[a.type] ?? 9) - (ORDRE_DU_JOUR[b.type] ?? 9));
}

/** Les cartes du jour d'un message, en bloc (posé avant la question qui les a fait naître, comme l'accueil). */
export function BlocDuJour({ m }: { m: MessageFil }) {
  const cartes = cartesDuJour(m);
  if (!cartes.length) return null;
  return (
    <div className="bulle-groupe assistant" data-jour>
      {cartes.map((c) => (
        <Carte key={c.id} carte={c} contexte={{ cleMessage: m.cle }} />
      ))}
    </div>
  );
}

export function Message({ m, question, sansJour = false }: { m: MessageFil; question?: MessageFil; sansJour?: boolean }) {
  const assistant = useAssistant();
  if (m.role === "personne") {
    return (
      <div className="bulle-groupe moi" data-message={m.id ?? m.cle}>
        <div className="bulle moi">
          {m.voix && <Mic className="mr-1.5 inline size-3.5 align-[-2px] opacity-80" aria-label="dicté" />}
          {m.texte}
          {!!m.pieces?.length && (
            <span className="mt-1 flex flex-wrap gap-1.5 text-sm opacity-90">
              {m.pieces.map((p, i) => (
                <span key={i} className="inline-flex items-center gap-1">
                  <Paperclip className="size-3.5" />
                  {p.nom}
                </span>
              ))}
            </span>
          )}
        </div>
        {m.masquage ? (
          <p className="transmis" data-masquage>
            <Lock className="size-3.5" aria-hidden />
            <span>
              Transmis à {assistant} : <q>{m.masquage.texte}</q>
            </span>
          </p>
        ) : (
          RE_MASQUE.test(m.texte) && (
            <p className="transmis" data-masquage>
              <Lock className="size-3.5" aria-hidden />
              <span>Donnée sensible masquée avant l’envoi à {assistant}</span>
            </p>
          )
        )}
        <div className="meta-message">
          {heureDe(m.ts)}
          {m.ecran && NOMS_ECRANS[m.ecran] ? ` · depuis ${NOMS_ECRANS[m.ecran]}` : ""}
        </div>
      </div>
    );
  }
  const brut = m.enCours ? nettoyerFlux(m.texte) : m.texte;
  // Message spontané (brief, routine) dont la carte reprend le texte : seule la carte s'affiche (pas de doublon).
  const repris =
    m.spontane &&
    m.cartes.some((c) => (c.type === "brief" || c.type === "routine_resultat") && typeof c.donnees.texte === "string" && c.donnees.texte.trim() === brut.trim());
  const texte = repris ? "" : brut;
  const couvertes = new Set(m.cartes.flatMap((c) => c.actions?.map((a) => a.confirmation) ?? []).filter((x) => typeof x === "number"));
  const confirmations = m.confirmations.filter((c) => !couvertes.has(c.id));
  const systeme = m.role === "systeme";
  const duJour = sansJour ? [] : cartesDuJour(m);
  const autres = m.cartes.filter((c) => !TYPES_DU_JOUR.has(c.type));
  return (
    <div className="bulle-groupe assistant" data-message={m.id ?? m.cle}>
      {duJour.map((c) => (
        <Carte key={c.id} carte={c} contexte={{ cleMessage: m.cle }} />
      ))}
      {m.enCours ? <ReflexionEnCours m={m} /> : <ReflexionFinie m={m} />}
      {texte.trim() && (
        <div className={`bulle ${systeme ? "systeme" : "assistant"}`}>
          <TexteRiche texte={texte} />
        </div>
      )}
      {autres.map((c) => (
        <Carte key={c.id} carte={c} contexte={{ cleMessage: m.cle }} />
      ))}
      {confirmations.map((c) => (
        <ConfirmationEnLigne key={c.id} c={c} />
      ))}
      {m.erreur && (
        <div className="bulle systeme flex items-start gap-2" role="alert">
          <AlertCircle className="mt-0.5 size-4 flex-none text-danger" />
          {m.erreur}
        </div>
      )}
      {m.attente && !m.enCours && <div className="text-sm text-muted-foreground">{m.attente}</div>}
      {!m.enCours && !systeme && (texte.trim() || autres.length > 0) && (
        <div className="pied-reponse">
          <div className="meta-message">
            {assistant} · {heureDe(m.ts)}
          </div>
          {texte.trim() && <BarreReponse m={m} texte={texte} question={question} />}
        </div>
      )}
    </div>
  );
}

// Routines : ce qui tourne en tâche de fond (briefs du foyer, crons des capacités). Changer un horaire prépare une
// action à valider (propriétaires, preuve d'identité) ; tout se règle aussi en parlant.
import { useEffect, useState } from "react";
import { Pause } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ConfirmationEnLigne } from "@/components/cartes/actions";
import { api, messageErreur } from "@/lib/api";
import { useAssistant } from "@/lib/session";
import { toast } from "@/lib/toasts";
import type { Confirmation } from "@/lib/types";
import { magasin } from "@/store/conversation";
import { TitrePage, Vide } from "./commun";

interface Routine {
  capacite: string;
  nom: string;
  titre: string;
  quand: string;
  lisible: string;
  statut: string;
}

const JOURS = ["tous les jours", "du lundi au vendredi", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"];
const humain = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function Horaire({ r, modifiable, onConfirmation }: { r: Routine; modifiable: boolean; onConfirmation: (c: Confirmation) => void }) {
  const [ouvert, setOuvert] = useState(false);
  const [jour, setJour] = useState("tous les jours");
  const [heure, setHeure] = useState("07:00");
  const [envoi, setEnvoi] = useState(false);
  if (!modifiable) return null;
  if (!ouvert)
    return (
      <Button size="sm" variant="outline" onClick={() => setOuvert(true)}>
        Changer l’horaire
      </Button>
    );
  const valider = async () => {
    setEnvoi(true);
    try {
      const [h, m] = heure.split(":");
      const quand = `${jour} ${Number(h)}h${m === "00" ? "" : m}`;
      const res = await api<{ confirmation: Confirmation }>("PATCH", `/api/routines/${encodeURIComponent(r.capacite)}/${encodeURIComponent(r.nom)}`, { quand });
      onConfirmation(res.confirmation);
      setOuvert(false);
    } catch (e) {
      toast(messageErreur(e));
    } finally {
      setEnvoi(false);
    }
  };
  return (
    <div className="flex w-full flex-wrap items-center gap-2">
      <select className="liste-deroulante petit w-auto" value={jour} onChange={(e) => setJour(e.target.value)} aria-label={`Jours : ${r.titre}`}>
        {JOURS.map((j) => (
          <option key={j} value={j}>
            {humain(j)}
          </option>
        ))}
      </select>
      <input type="time" className="champ petit w-[110px]" value={heure} onChange={(e) => setHeure(e.target.value)} aria-label="Heure" />
      <Button size="sm" variant="default" disabled={envoi} onClick={valider}>
        Préparer
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOuvert(false)}>
        Annuler
      </Button>
    </div>
  );
}

export function EcranRoutines() {
  const assistant = useAssistant();
  const [donnees, setDonnees] = useState<{ routines: Routine[]; pause: boolean; modifiable: boolean } | null>(null);
  const [erreur, setErreur] = useState("");
  const [confirmations, setConfirmations] = useState<Record<string, Confirmation>>({});
  useEffect(() => {
    api<{ routines: Routine[]; pause: boolean; modifiable: boolean }>("GET", "/api/routines")
      .then(setDonnees)
      .catch((e) => setErreur(messageErreur(e)));
  }, []);
  const groupes = new Map<string, Routine[]>();
  for (const r of donnees?.routines ?? []) {
    const g = r.capacite === "foyer" ? "Rendez-vous du foyer" : r.titre;
    groupes.set(g, [...(groupes.get(g) ?? []), r]);
  }
  const contexte = { ecran: "routines", objet: "routines" };
  return (
    <>
      <TitrePage titre="Routines" sous={`Elles tournent en tâche de fond ; ${assistant} n’en parle que s’il a quelque chose d’utile. Tu peux aussi les régler à la voix.`} />
      {erreur && <Vide>{erreur}</Vide>}
      {donnees?.pause && (
        <div className="mb-4 flex flex-wrap items-center gap-3 rounded-md bg-muted px-3 py-2.5 text-sm">
          <Pause className="size-4" />
          <span className="flex-1">En pause : rappels et validations suspendus.</span>
          <Button size="sm" variant="outline" onClick={() => void magasin.envoyer("Reprends les rappels et les routines.", { contexte })}>
            Reprendre
          </Button>
        </div>
      )}
      {donnees && !donnees.routines.length && <Vide>Aucune routine programmée.</Vide>}
      {groupes.size > 0 && (
        <div className="rounded-lg border px-4 pb-1">
          {[...groupes.entries()].map(([g, liste]) => (
            <section key={g}>
              <h3 className="rayon-t pt-4 pb-0.5">{g}</h3>
              <div className="liste">
                {liste.map((r) => {
                  const cle = `${r.capacite}/${r.nom}`;
                  const actif = r.statut === "actif";
                  return (
                    <div className="ligne flex-wrap" key={cle}>
                      <Switch
                        checked={actif}
                        aria-label={`Routine ${r.capacite === "foyer" ? r.titre : humain(r.nom)}`}
                        disabled={!donnees?.modifiable}
                        onCheckedChange={(v) =>
                          void magasin.envoyer(`${v ? "Réactive" : "Mets en pause"} la routine « ${r.capacite === "foyer" ? r.titre : `${humain(r.nom)} (${r.titre})`} ».`, { contexte })
                        }
                      />
                      <div className={`min-w-[170px] flex-1 ${actif ? "" : "opacity-50"}`}>
                        <div className="ligne-t">{r.capacite === "foyer" ? r.titre : humain(r.nom)}</div>
                        <div className="ligne-s">{r.lisible || r.quand}</div>
                      </div>
                      <Horaire r={r} modifiable={!!donnees?.modifiable} onConfirmation={(c) => setConfirmations((x) => ({ ...x, [cle]: c }))} />
                      {confirmations[cle] && (
                        <div className="w-full">
                          <ConfirmationEnLigne c={{ ...confirmations[cle], step_up: true }} />
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </>
  );
}

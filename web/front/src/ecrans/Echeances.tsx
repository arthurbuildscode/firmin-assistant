// Échéances et anniversaires : point d'accès direct `GET /api/echeances` ({echeances, anniversaires}), sinon la
// carte « À noter » (du fil ou du jour). Chaque échéance porte sa raison d'agir ; les actions proposées (comparer
// des offres, chercher un créneau) passent le filtre « ça vaut le coup » côté assistant.
import { useState } from "react";
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { dateLocale, moisCourt } from "@/lib/dates";
import { preremplirDock } from "@/lib/dock";
import { listeDe, useDonnees } from "@/lib/donnees";
import { useAssistant } from "@/lib/session";
import { derniereCarte, magasin, useConversation } from "@/store/conversation";
import { DateColonne, TitrePage, Vide } from "./commun";

type ActionEcheance = { libelle: string; texte?: string };
type Ligne = {
  id?: number | string;
  type?: string;
  titre?: string;
  date?: string;
  dans_jours?: number;
  raison?: string;
  notes?: string;
  categorie?: string;
  rappels?: string;
  age?: number;
  en_retard?: boolean;
  actions?: ActionEcheance[];
};
type Donnees = { echeances: Ligne[]; anniversaires: Ligne[] };

// `GET /api/echeances` → `{echeances: [{id, titre, date, prochaine, dans_jours, en_retard, notes, …}]}` : la prochaine
// occurrence fait foi, les notes portent la raison d'agir. Les anniversaires viennent de la carte « À noter ».
const valider = (brut: unknown): Donnees | null => {
  const echeances = listeDe<Ligne & { prochaine?: string }>(brut, "echeances");
  if (!echeances) return null;
  return {
    echeances: echeances.map((e) => ({ ...e, type: "echeance", date: e.prochaine || e.date, raison: e.raison || e.notes })),
    anniversaires: listeDe<Ligne>(brut, "anniversaires") ?? [],
  };
};

function dateLongue(iso?: string): string {
  const d = dateLocale(String(iso || ""));
  return d ? d.toLocaleDateString("fr-FR", { weekday: "short", day: "numeric", month: "short" }).replace(/^./, (c) => c.toUpperCase()) : "";
}

function LigneEcheance({ e, contexte }: { e: Ligne; contexte: { ecran: string; objet: string } }) {
  const d = dateLocale(String(e.date || ""));
  const enCours = useConversation((x) => x.echangeEnCours);
  const raison = e.raison || e.notes || (e.type === "anniversaire" && e.age ? `${e.age} ans` : "");
  const sous = [dateLongue(e.date), raison].filter(Boolean).join(" · ");
  return (
    <div className="ligne-ech" data-echeance>
      <DateColonne jour={d?.getDate() ?? "?"} sous={d ? moisCourt(d) : undefined} petit={typeof e.dans_jours === "number" ? (e.en_retard || e.dans_jours < 0 ? "en retard" : `J-${e.dans_jours}`) : undefined} />
      <div className="min-w-0 flex-1">
        <div className="ligne-t">{e.titre}</div>
        {sous && <div className="ligne-s">{sous}</div>}
        {e.categorie && <div className="ligne-s">{e.categorie}</div>}
        {!!e.actions?.length && (
          <div className="boutons mt-2">
            {e.actions.map((a) => (
              <Button key={a.libelle} size="sm" variant="outline" disabled={enCours} onClick={() => void magasin.envoyer(a.texte || `${a.libelle} : ${e.titre}`, { contexte })}>
                {a.libelle}
              </Button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export function EcranEcheances() {
  const assistant = useAssistant();
  const messages = useConversation((e) => e.messages);
  const accueil = useConversation((e) => e.accueil);
  const direct = useDonnees("echeances", valider);
  const [onglet, setOnglet] = useState<"echeances" | "anniversaires">("echeances");
  const contexte = { ecran: "echeances", objet: "échéances et anniversaires" };

  const aNoter = derniereCarte(messages, ["a_noter"]) ?? accueil.find((c) => c.type === "a_noter") ?? null;
  const elements: Ligne[] = Array.isArray(aNoter?.donnees.elements) ? aNoter!.donnees.elements : [];
  const anniversairesCarte = elements.filter((x) => x.type === "anniversaire");
  let donnees: Donnees | null = direct.statut === "ok" ? direct.donnees : null;
  if (donnees && !donnees.anniversaires.length) donnees = { ...donnees, anniversaires: anniversairesCarte };
  if (!donnees && direct.statut === "absent" && aNoter) donnees = { echeances: elements.filter((x) => x.type !== "anniversaire"), anniversaires: anniversairesCarte };
  const lignes = donnees ? donnees[onglet] : [];
  return (
    <>
      <TitrePage titre="Échéances et anniversaires" sous="Rappels avant chaque échéance, avec la raison d’agir · cadeaux en avance" />
      <div className="mb-5 flex flex-wrap gap-2">
        <Button variant="default" onClick={() => preremplirDock("Ajoute une échéance : ")}>
          <Plus />
          Ajouter une échéance
        </Button>
      </div>
      {donnees && (
        <div className="seg seg-large mb-4 max-w-[720px]" role="group" aria-label="Échéances ou anniversaires">
          {(["echeances", "anniversaires"] as const).map((o) => (
            <button key={o} aria-pressed={onglet === o} onClick={() => setOnglet(o)}>
              {o === "echeances" ? "Échéances" : "Anniversaires"} <span className="num text-muted-foreground">{donnees[o].length}</span>
            </button>
          ))}
        </div>
      )}
      {donnees && onglet === "echeances" && (
        <p className="mb-4 max-w-[68ch] text-sm text-muted-foreground">
          <b className="font-semibold text-foreground">Filtre « ça vaut le coup ».</b> {assistant} ne propose une action que si elle fait gagner du temps ou de
          l’argent, et demande toujours avant de réserver ou de résilier.
        </p>
      )}
      {direct.statut === "chargement" ? null : !donnees ? (
        <Vide>Aucune échéance dans les deux semaines à venir.</Vide>
      ) : lignes.length === 0 ? (
        <Vide>{onglet === "echeances" ? "Aucune échéance à venir." : "Aucun anniversaire à venir."}</Vide>
      ) : (
        <div className="liste max-w-[720px]">
          {lignes.map((e, i) => (
            <LigneEcheance key={e.id ?? i} e={e} contexte={contexte} />
          ))}
        </div>
      )}
    </>
  );
}

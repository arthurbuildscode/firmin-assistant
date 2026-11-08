// Repas : le planning de la semaine en plein écran (repas maison, Kitchen Daily, invités, restaurant), puis la
// prochaine livraison en carte compacte, riche au geste (vignettes réelles du site quand la source en fournit).
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Carte } from "@/components/cartes/Carte";
import { PlanningRepas } from "@/components/cartes/maison";
import { isoJour } from "@/lib/dates";
import { preremplirDock } from "@/lib/dock";
import { derniereCarte, useConversation } from "@/store/conversation";
import { BoutonActualiser, TitrePage, Vide } from "./commun";

export function EcranRepas() {
  const messages = useConversation((e) => e.messages);
  const planning = derniereCarte(messages, ["repas_semaine"]);
  const proposition = derniereCarte(messages, ["kitchen_daily_proposition"]);
  const extras = derniereCarte(messages, ["kitchen_daily_extras"]);
  const invites = derniereCarte(messages, ["menu_invites"]);
  const contexte = { ecran: "repas", objet: "planning des repas" };
  const rien = !planning && !proposition && !extras && !invites;
  const jours = Array.isArray(planning?.donnees.jours) ? planning!.donnees.jours : [];
  return (
    <>
      <TitrePage titre="Repas" sous="Planning de la semaine · Kitchen Daily n’est qu’une des sources" />
      {rien ? (
        <Vide action={<BoutonActualiser texte="Montre-moi le planning des repas de la semaine." contexte={contexte} libelle="Afficher le planning" />}>
          Le planning des repas et les propositions s’affichent ici dès que tu les demandes.
        </Vide>
      ) : (
        <>
          <div className="mb-5 flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => preremplirDock("Change le repas de ")}>
              <RefreshCw />
              Changer un repas
            </Button>
          </div>
          <div className="flex max-w-[900px] flex-col gap-6">
            {jours.length > 0 && (
              <section aria-label="Planning des repas">
                <PlanningRepas jours={jours} aujourdhui={isoJour(new Date())} />
              </section>
            )}
            {proposition && <Carte carte={{ ...proposition, donnees: { ...proposition.donnees, compact: true } }} contexte={{ ecran: "repas" }} />}
            {extras && <Carte carte={extras} contexte={{ ecran: "repas" }} />}
            {invites && <Carte carte={invites} contexte={{ ecran: "repas" }} />}
          </div>
        </>
      )}
    </>
  );
}

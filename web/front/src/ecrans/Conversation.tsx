// Accueil = le fil de conversation : carte du jour (sans le modèle, GET /api/brief/jour), puis les échanges.
import { Fragment, useEffect, useLayoutEffect, useRef } from "react";
import { Loader2 } from "lucide-react";
import { Carte } from "@/components/cartes/Carte";
import { BlocDuJour, cartesDuJour, Message } from "@/components/Message";
import { PropositionsContexte } from "@/components/Propositions";
import { isoJour } from "@/lib/dates";
import { useAssistant } from "@/lib/session";
import type { MessageFil } from "@/lib/types";
import { magasin, useConversation } from "@/store/conversation";

const ordreJour = (t: string) => ({ bonjour: 0, a_noter: 1, brief: 2 })[t] ?? 3;

export function EcranConversation() {
  const assistant = useAssistant();
  const messages = useConversation((e) => e.messages);
  const charge = useConversation((e) => e.charge);
  const erreur = useConversation((e) => e.erreurChargement);
  const accueil = useConversation((e) => e.accueil);
  const accueilDate = useConversation((e) => e.accueilDate);
  const fin = useRef<HTMLDivElement>(null);
  const premier = useRef(true);
  const aujourdhui = isoJour(new Date());

  useEffect(() => {
    if (!accueilDate) void magasin.chargerAccueil();
  }, [accueilDate]);

  // Messages spontanés arrivés pendant qu'un autre écran était ouvert : vus en ouvrant le fil.
  const nonLus = useConversation((e) => e.nonLus);
  useEffect(() => {
    if (nonLus) magasin.marquerLus();
  }, [nonLus]);

  // Le cœur produit aussi les cartes du jour au premier message du jour : pas de doublon dans le fil.
  const dejaDansLeFil = messages.some((m) => m.cartes.some((c) => c.type === "bonjour" && c.donnees.date === aujourdhui));
  const nb = messages.length;
  const dernier = messages[nb - 1];
  const signature = `${nb}:${dernier?.texte.length ?? 0}:${dernier?.cartes.length ?? 0}:${dernier?.etapes.length ?? 0}`;

  useLayoutEffect(() => {
    if (!charge) return;
    const bas = document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 260;
    if (premier.current || bas || dernier?.enCours) {
      fin.current?.scrollIntoView({ block: "end", behavior: premier.current ? "auto" : "smooth" });
      premier.current = false;
    }
  }, [signature, charge, dernier?.enCours]);

  return (
    <div className="fil" aria-label="Conversation">
      {!dejaDansLeFil && accueil.length > 0 && (
        <div className="bulle-groupe assistant" data-accueil>
          {[...accueil].sort((a, b) => ordreJour(a.type) - ordreJour(b.type)).map((c) => (
            <Carte key={c.id} carte={c} />
          ))}
        </div>
      )}
      {!charge && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
          <Loader2 className="size-4 animate-spin" />
          Chargement de la conversation…
        </div>
      )}
      {erreur && <div className="vide">{erreur}</div>}
      {charge && !messages.length && !accueil.length && (
        <p className="text-muted-foreground">Écris à {assistant} : agenda, courses, repas, échéances, documents…</p>
      )}
      {messages.map((m, i) => {
        const suivant = messages[i + 1];
        const precedent = messages[i - 1];
        // Les cartes du jour d'une réponse sont posées avant la question qui les a fait naître : le fil s'ouvre
        // toujours sur la journée (maquette), même quand la première demande du jour vient d'un écran.
        const jourHisse = (x?: MessageFil, avant?: MessageFil) => !!x && x.role !== "personne" && avant?.role === "personne" && cartesDuJour(x).length > 0;
        return (
          <Fragment key={m.cle}>
            {m.role === "personne" && jourHisse(suivant, m) && <BlocDuJour m={suivant!} />}
            <Message m={m} sansJour={jourHisse(m, precedent)} question={m.role !== "personne" && precedent?.role === "personne" ? precedent : undefined} />
          </Fragment>
        );
      })}
      {charge && <PropositionsContexte messages={messages} />}
      <div ref={fin} />
    </div>
  );
}

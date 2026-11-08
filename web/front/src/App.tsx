// Racine : session (passkey), préférences de la personne, magasin de conversation, écrans.
import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Coquille } from "@/components/Coquille";
import { FournisseurInfoBulles } from "@/components/ui/tooltip";
import { api, oublierStepUp, quandDeconnecte } from "@/lib/api";
import { chargerPreferences, useSuiviTheme, appliquerApparence } from "@/lib/preferences";
import { viderCacheHorsLigne } from "@/lib/push";
import { naviguer, useEcran } from "@/lib/routeur";
import { definirSession } from "@/lib/session";
import type { Session } from "@/lib/types";
import { magasin, useConversation } from "@/store/conversation";
import { EcranConnexion } from "@/ecrans/Connexion";
import { EcranConversation } from "@/ecrans/Conversation";
import { EcranCourses } from "@/ecrans/Courses";
import { EcranEcheances } from "@/ecrans/Echeances";
import { EcranFichiers, EcranJournal, EcranMemoire } from "@/ecrans/Famille";
import { EcranPlus } from "@/ecrans/Plus";
import { EcranReglages } from "@/ecrans/Reglages";
import { EcranRepas } from "@/ecrans/Repas";
import { EcranRoutines } from "@/ecrans/Routines";
import { EcranSemaine } from "@/ecrans/Semaine";
import { derniereCarte } from "@/store/conversation";

function useObjetEcran(ecran: string): string | undefined {
  // Ce que la personne a sous les yeux, envoyé avec ses demandes (« contexte.objet ») : court, sans identifiant.
  const messages = useConversation((e) => e.messages);
  if (ecran === "courses") {
    const c = derniereCarte(messages, ["courses"]);
    return c ? `liste de courses, ${c.donnees.restants ?? "?"} articles à acheter` : "liste de courses";
  }
  if (ecran === "repas") return "planning des repas de la semaine";
  if (ecran === "semaine") return "agenda des sept prochains jours";
  return undefined;
}

export default function App() {
  const [etat, setEtat] = useState<"chargement" | "connexion" | "pret">("chargement");
  const ecran = useEcran();
  const objet = useObjetEcran(ecran);
  useSuiviTheme();

  const demarrer = useCallback(async () => {
    try {
      const s = await api<Session>("GET", "/auth/session");
      if (!s.connecte || !s.personne) {
        appliquerApparence();
        setEtat("connexion");
        return;
      }
      definirSession(s);
      chargerPreferences(s.personne.linux_user);
      setEtat("pret");
      void magasin.demarrer();
    } catch {
      appliquerApparence();
      setEtat("connexion");
    }
  }, []);

  const deconnecter = useCallback(async (appelServeur = true) => {
    if (appelServeur) await api("POST", "/auth/deconnexion", {}).catch(() => {});
    oublierStepUp();
    magasin.arreter();
    definirSession({ connecte: false });
    await viderCacheHorsLigne();
    setEtat("connexion");
    naviguer("conversation", true);
  }, []);

  useEffect(() => {
    void demarrer();
    const fin = quandDeconnecte(() => void deconnecter(false));
    // Clic sur une notification : le service worker demande d'ouvrir un écran.
    const surMessage = (e: MessageEvent) => {
      if (e.data?.type === "ouvrir") naviguer(new URL(e.data.url, location.origin).pathname.replace(/^\//, "") || "conversation");
    };
    navigator.serviceWorker?.addEventListener("message", surMessage);
    const enLigne = () => {
      magasin.definirHorsLigne(false);
      void magasin.listerConversations();
    };
    const horsLigne = () => magasin.definirHorsLigne(true);
    window.addEventListener("online", enLigne);
    window.addEventListener("offline", horsLigne);
    return () => {
      fin();
      navigator.serviceWorker?.removeEventListener("message", surMessage);
      window.removeEventListener("online", enLigne);
      window.removeEventListener("offline", horsLigne);
    };
  }, [demarrer, deconnecter]);

  if (etat === "chargement")
    return (
      <div className="grid min-h-dvh place-items-center text-muted-foreground" role="status">
        <Loader2 className="size-5 animate-spin" />
      </div>
    );
  if (etat === "connexion") return <EcranConnexion onConnecte={() => void demarrer()} />;

  const ecrans: Record<string, React.ReactNode> = {
    conversation: <EcranConversation />,
    semaine: <EcranSemaine />,
    courses: <EcranCourses />,
    repas: <EcranRepas />,
    echeances: <EcranEcheances />,
    journal: <EcranJournal />,
    fichiers: <EcranFichiers />,
    memoire: <EcranMemoire />,
    routines: <EcranRoutines />,
    reglages: <EcranReglages onDeconnexion={() => void deconnecter()} />,
    plus: <EcranPlus />,
  };
  return (
    <FournisseurInfoBulles>
      <Coquille ecran={ecran} objet={objet} onDeconnexion={() => void deconnecter()}>
        {ecrans[ecran]}
      </Coquille>
    </FournisseurInfoBulles>
  );
}

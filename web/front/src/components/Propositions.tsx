// Permissions proposées en contexte (skill ux-capacites §7) : jamais à aller chercher dans Réglages. Dans le fil, une
// seule fois par personne et par appareil : la carte « Tu veux que je te prévienne ici ? » après le premier échange
// réussi (demande de notifications du navigateur), puis une proposition discrète d'installer l'application après
// quelques messages. « Plus tard » n'est pas reproposé ici.
import { useEffect, useState, useSyncExternalStore } from "react";
import { BellRing, Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { messageErreur } from "@/lib/api";
import { activerNotifications, pushDisponible } from "@/lib/push";
import { useAssistant, useSession } from "@/lib/session";
import { ecrire, lire } from "@/lib/stockage";
import { toast } from "@/lib/toasts";
import type { MessageFil } from "@/lib/types";

// --- invite d'installation du navigateur (Chrome, Edge, Android) ----------------------------------------------------

type InviteInstallation = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };
let invite: InviteInstallation | null = null;
const ecouteurs = new Set<() => void>();
if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault(); // pas de bandeau du navigateur : l'assistant le propose au bon moment
    invite = e as InviteInstallation;
    ecouteurs.forEach((f) => f());
  });
  window.addEventListener("appinstalled", () => {
    invite = null;
    ecouteurs.forEach((f) => f());
  });
}
const useInvite = () =>
  useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => invite,
  );

function dejaInstallee(): boolean {
  try {
    return window.matchMedia("(display-mode: standalone)").matches || (navigator as unknown as { standalone?: boolean }).standalone === true;
  } catch {
    return false;
  }
}
const iOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent);

// --- état « déjà proposé » ------------------------------------------------------------------------------------------

type Choix = "oui" | "plus_tard";
const cle = (personne: string, quoi: string) => `homeos:propose:${quoi}:${personne}`;

export const MESSAGES_AVANT_INSTALLATION = 4;

function echangeReussi(messages: MessageFil[]): boolean {
  return messages.some((m) => m.role === "assistant" && !m.enCours && !m.erreur && (m.texte.trim() || m.cartes.length > 0) && m.id !== undefined);
}

export function PropositionsContexte({ messages }: { messages: MessageFil[] }) {
  const assistant = useAssistant();
  const session = useSession();
  const personne = session.personne?.linux_user || "?";
  const installation = useInvite();
  const [notif, setNotif] = useState<Choix | null>(() => lire<Choix | null>(cle(personne, "notifications"), null));
  const [install, setInstall] = useState<Choix | null>(() => lire<Choix | null>(cle(personne, "installation"), null));
  const [occupe, setOccupe] = useState(false);
  const [permission, setPermission] = useState(() => (typeof Notification !== "undefined" ? Notification.permission : "denied"));

  useEffect(() => {
    setNotif(lire<Choix | null>(cle(personne, "notifications"), null));
    setInstall(lire<Choix | null>(cle(personne, "installation"), null));
  }, [personne]);

  const enCours = messages.some((m) => m.enCours);
  const demandes = messages.filter((m) => m.role === "personne").length;
  const proposerNotif = !enCours && notif === null && pushDisponible() && permission === "default" && echangeReussi(messages);
  const proposerInstall =
    !enCours && !proposerNotif && install === null && demandes >= MESSAGES_AVANT_INSTALLATION && !dejaInstallee() && (!!installation || iOS());

  const choisirNotif = async (oui: boolean) => {
    ecrire(cle(personne, "notifications"), oui ? "oui" : "plus_tard");
    if (!oui) {
      setNotif("plus_tard");
      return;
    }
    setOccupe(true);
    try {
      await activerNotifications();
      toast("C’est réglé : le brief et les validations arriveront ici.");
    } catch (e) {
      toast(messageErreur(e));
    } finally {
      setOccupe(false);
      setNotif("oui");
      if (typeof Notification !== "undefined") setPermission(Notification.permission);
    }
  };

  const choisirInstall = async (oui: boolean) => {
    ecrire(cle(personne, "installation"), oui ? "oui" : "plus_tard");
    setInstall(oui ? "oui" : "plus_tard");
    if (!oui) return;
    if (installation) {
      await installation.prompt().catch(() => {});
      invite = null;
    } else if (iOS()) toast("Dans Safari : bouton Partager, puis « Sur l’écran d’accueil ».", 7000);
  };

  if (proposerNotif)
    return (
      <div className="bulle-groupe assistant" data-proposition="notifications">
        <section className="carte proposition">
          <div className="flex items-start gap-2.5">
            <BellRing className="mt-0.5 size-4 flex-none text-muted-foreground" aria-hidden />
            <p className="text-sm">Tu veux que je te prévienne ici (brief, validations) ?</p>
          </div>
          <div className="boutons">
            <Button size="sm" variant="default" disabled={occupe} onClick={() => void choisirNotif(true)}>
              Oui
            </Button>
            <Button size="sm" variant="ghost" disabled={occupe} onClick={() => void choisirNotif(false)}>
              Plus tard
            </Button>
          </div>
        </section>
      </div>
    );
  if (proposerInstall)
    return (
      <div className="proposition-discrete" data-proposition="installation">
        <Download className="size-4 flex-none" aria-hidden />
        <span className="min-w-0 flex-1">Installer {assistant} sur cet appareil, comme une application</span>
        <Button size="sm" variant="ghost" onClick={() => void choisirInstall(true)}>
          Installer
        </Button>
        <button type="button" className="bouton-mini" aria-label="Plus tard" onClick={() => void choisirInstall(false)}>
          <X />
        </button>
      </div>
    );
  return null;
}

// Bandeau discret des sessions de secours (code Telegram) : 24 h, conversation et lecture seulement ; l'ajout d'une
// passkey rétablit une session complète (CONTRACT §12).
import { useState } from "react";
import { Fingerprint } from "lucide-react";
import { inscrire, messageErreur } from "@/lib/api";
import { naviguer } from "@/lib/routeur";
import { rafraichirSession, useSession } from "@/lib/session";
import { toast } from "@/lib/toasts";
import { passkeysDisponibles } from "@/lib/webauthn";

export function BandeauSecours() {
  const session = useSession();
  const [occupe, setOccupe] = useState(false);
  if (session.niveau !== "secours") return null;
  const ajouter = async () => {
    if (!passkeysDisponibles()) {
      naviguer("reglages");
      return;
    }
    setOccupe(true);
    try {
      await inscrire("");
      await rafraichirSession();
      toast("Passkey ajoutée : connexion complète.");
    } catch (e) {
      toast(messageErreur(e));
    } finally {
      setOccupe(false);
    }
  };
  return (
    <button
      type="button"
      className="mb-4 flex w-full items-center gap-2 rounded-md bg-muted px-3 py-2 text-left text-sm text-muted-foreground"
      onClick={() => void ajouter()}
      disabled={occupe}
    >
      <Fingerprint className="size-4 flex-none" />
      <span>Connexion de secours · ajoute une passkey</span>
    </button>
  );
}

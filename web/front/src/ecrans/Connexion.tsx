// Connexion : passkey (empreinte, visage), activation d'un appareil par lien d'invitation (#jeton, 10 min),
// repli par code reçu sur Telegram.
import { useState } from "react";
import { Fingerprint, House, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, connecter, inscrire, messageErreur } from "@/lib/api";
import { passkeysDisponibles } from "@/lib/webauthn";

export function EcranConnexion({ onConnecte }: { onConnecte: () => void }) {
  const invitation = location.pathname === "/inscription" ? location.hash.slice(1) : "";
  const assistant = document.title && !document.title.startsWith("__") ? document.title : "";
  const [occupe, setOccupe] = useState<"" | "passkey" | "code">("");
  const [erreur, setErreur] = useState("");
  const [info, setInfo] = useState("");
  const [code, setCode] = useState(false);
  const [prenom, setPrenom] = useState("");
  const [valeur, setValeur] = useState("");

  const agir = async (type: "passkey" | "code", fn: () => Promise<unknown>) => {
    setOccupe(type);
    setErreur("");
    try {
      await fn();
      if (invitation) history.replaceState(null, "", "/");
      onConnecte();
    } catch (e) {
      setErreur(messageErreur(e));
    } finally {
      setOccupe("");
    }
  };

  return (
    <main className="mx-auto flex min-h-dvh max-w-[400px] flex-col justify-center gap-6 px-4 py-10">
      <div className="flex items-center gap-3">
        <span className="logo size-10 rounded-[10px]">
          <House className="!size-5" />
        </span>
        <h1>{assistant || "Connexion"}</h1>
      </div>
      {!passkeysDisponibles() && <p className="text-sm text-danger">Ce navigateur ne gère pas les passkeys : utilise le code Telegram.</p>}
      {invitation ? (
        <div className="flex flex-col gap-3">
          <p className="text-muted-foreground">Active cet appareil : il te demandera ton empreinte, ton visage ou le code de l’appareil.</p>
          <Button size="lg" variant="default" disabled={!!occupe} onClick={() => agir("passkey", () => inscrire(invitation))} data-action="inscription">
            {occupe === "passkey" ? <Loader2 className="animate-spin" /> : <Fingerprint />}
            Activer cet appareil
          </Button>
        </div>
      ) : (
        <Button size="lg" variant="default" disabled={!!occupe} onClick={() => agir("passkey", connecter)} data-action="connexion">
          {occupe === "passkey" ? <Loader2 className="animate-spin" /> : <Fingerprint />}
          Se connecter
        </Button>
      )}
      {erreur && (
        <p className="text-sm text-danger" role="alert">
          {erreur}
        </p>
      )}
      {!code ? (
        <Button variant="ghost" className="self-start -ml-3 text-muted-foreground" onClick={() => setCode(true)}>
          Pas de passkey sur cet appareil ? Recevoir un code sur Telegram
        </Button>
      ) : (
        <form
          className="flex flex-col gap-3 rounded-lg border p-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!info) {
              setOccupe("code");
              api<{ message: string }>("POST", "/auth/code/demande", { personne: prenom })
                .then((r) => setInfo(r.message))
                .catch((er) => setErreur(messageErreur(er)))
                .finally(() => setOccupe(""));
            } else void agir("code", () => api("POST", "/auth/code/verification", { personne: prenom, code: valeur }));
          }}
        >
          <label className="flex flex-col gap-1.5 text-sm font-medium">
            Prénom
            <input className="champ" value={prenom} onChange={(e) => setPrenom(e.target.value)} autoComplete="given-name" required />
          </label>
          {info && (
            <>
              <p className="text-sm text-muted-foreground">{info}</p>
              <label className="flex flex-col gap-1.5 text-sm font-medium">
                Code à 6 chiffres
                <input className="champ num" value={valeur} onChange={(e) => setValeur(e.target.value)} inputMode="numeric" autoComplete="one-time-code" maxLength={6} required />
              </label>
            </>
          )}
          <Button type="submit" variant="outline" disabled={!!occupe || !prenom.trim()}>
            {occupe === "code" && <Loader2 className="animate-spin" />}
            {info ? "Valider le code" : "Recevoir un code"}
          </Button>
        </form>
      )}
    </main>
  );
}

// Carte « identifiants » : saisie sécurisée vers le coffre, sous step-up. Le modèle n'est jamais dans la boucle :
// les valeurs restent dans l'état local de ce formulaire (jamais dans le magasin global, jamais journalisées) et sont
// effacées dès l'envoi. Lot 5c : `{cle, type, champs}` → POST /api/coffre ; secret manquant d'une capacité :
// `{capacite}` → POST /api/coffre/{capacite}.
import { useEffect, useId, useRef, useState } from "react";
import { Check, Fingerprint, KeyRound, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, messageErreur } from "@/lib/api";
import type { Carte } from "@/lib/types";

const LIBELLES: Record<string, string> = {
  identifiant: "Identifiant",
  mot_de_passe: "Mot de passe",
  url: "Adresse du site",
  titulaire: "Titulaire de la carte",
  numero: "Numéro de la carte",
  expiration: "Expiration (MM/AA)",
  cvc: "Cryptogramme",
  code: "Code",
  cle: "Clé",
  valeur: "Valeur",
};

const SECRETS = new Set(["mot_de_passe", "cvc", "code", "cle", "valeur", "numero"]);

function attributs(champ: string): React.InputHTMLAttributes<HTMLInputElement> {
  switch (champ) {
    case "identifiant":
      return { type: "text", autoComplete: "username", autoCapitalize: "none", spellCheck: false };
    case "mot_de_passe":
      return { type: "password", autoComplete: "new-password" };
    case "url":
      return { type: "url", inputMode: "url", autoComplete: "off", placeholder: "https://" };
    case "numero":
      return { type: "password", inputMode: "numeric", autoComplete: "off", maxLength: 23 };
    case "expiration":
      return { type: "text", inputMode: "numeric", autoComplete: "off", placeholder: "MM/AA", maxLength: 5 };
    case "cvc":
      return { type: "password", inputMode: "numeric", autoComplete: "off", maxLength: 4 };
    case "titulaire":
      return { type: "text", autoComplete: "off" };
    default:
      return { type: SECRETS.has(champ) ? "password" : "text", autoComplete: "off", spellCheck: false };
  }
}

const libelle = (champ: string) => LIBELLES[champ] || champ.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export function CarteIdentifiants({ carte }: { carte: Carte }) {
  const d = carte.donnees;
  const cle = typeof d.cle === "string" ? d.cle : "";
  const capacite = typeof d.capacite === "string" ? d.capacite : "";
  const [champs, setChamps] = useState<string[]>(Array.isArray(d.champs) ? d.champs : Array.isArray(d.champs_manquants) ? d.champs_manquants : []);
  const [renseignes, setRenseignes] = useState<string[]>([]);
  // Secret manquant d'une capacité : le titre affiché est celui du manifeste (revue sécurité F), pas celui du texte
  // d'un outil.
  const [titreManifeste, setTitreManifeste] = useState<string>("");
  const facultatifs: string[] = Array.isArray(d.facultatifs) ? d.facultatifs : [];
  const formulaire = useRef<HTMLFormElement>(null);
  const [etat, setEtat] = useState<"saisie" | "envoi" | "fait">("saisie");
  const [erreur, setErreur] = useState("");
  const [faits, setFaits] = useState<string[]>([]);
  const prefixe = useId();

  useEffect(() => {
    // Secret manquant d'une capacité : la liste des champs vient du manifeste (noms seulement, jamais de valeur).
    if (cle || !capacite) return;
    api<{ champs: string[]; renseignes: string[]; titre?: string }>("GET", `/api/coffre/${encodeURIComponent(capacite)}`)
      .then((r) => {
        setChamps(r.champs);
        setRenseignes(r.renseignes);
        setTitreManifeste(r.titre ? `Identifiants ${r.titre}` : "Identifiants");
      })
      .catch(() => {});
  }, [cle, capacite]);

  const envoyer = async (e: React.FormEvent) => {
    e.preventDefault();
    const f = formulaire.current;
    if (!f) return;
    // Valeurs lues dans le DOM au dernier moment : aucune copie dans un état React.
    const valeurs: Record<string, string> = {};
    for (const c of champs) {
      const v = (f.elements.namedItem(c) as HTMLInputElement | null)?.value ?? "";
      if (v) valeurs[c] = c === "numero" ? v.replace(/[\s-]/g, "") : v;
    }
    if (!Object.keys(valeurs).length) {
      setErreur("Renseigne au moins un champ.");
      return;
    }
    setEtat("envoi");
    setErreur("");
    try {
      const r = cle
        ? await api<{ champs: string[] }>("POST", "/api/coffre", { cle, champs: valeurs })
        : await api<{ champs: string[] }>("POST", `/api/coffre/${encodeURIComponent(capacite)}`, { champs: valeurs });
      f.reset();
      setFaits(r.champs ?? Object.keys(valeurs));
      setEtat("fait");
    } catch (err) {
      setErreur(messageErreur(err));
      setEtat("saisie");
    } finally {
      for (const k of Object.keys(valeurs)) delete valeurs[k];
    }
  };

  const titre = cle ? carte.titre || d.libelle || "Identifiants" : titreManifeste || "Identifiants";
  if (etat === "fait") {
    return (
      <section className="carte" data-carte="identifiants">
        <div className="carte-titre flex items-center gap-2">
          <KeyRound className="size-4 text-muted-foreground" />
          {titre}
        </div>
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <Check className="size-4 text-good" />
          Enregistré dans le coffre ({faits.map(libelle).join(", ").toLowerCase()}).
        </p>
        <Button size="sm" variant="ghost" className="-ml-3 self-start" onClick={() => setEtat("saisie")}>
          Modifier
        </Button>
      </section>
    );
  }
  return (
    <section className="carte" data-carte="identifiants">
      <div className="carte-titre flex items-center gap-2">
        <KeyRound className="size-4 text-muted-foreground" />
        {titre}
      </div>
      <p className="-mt-1 text-xs text-muted-foreground">
        {d.type === "carte_bancaire"
          ? "Carte virtuelle à plafond uniquement. Chiffrée dans le coffre du foyer ; seuls les 4 derniers chiffres seront affichés."
          : "Chiffré dans le coffre du foyer. Jamais lu par l’assistant ni envoyé dans la conversation."}
      </p>
      <form ref={formulaire} className="flex flex-col gap-3" onSubmit={envoyer} autoComplete="off">
        {champs.map((c) => (
          <label key={c} className="flex flex-col gap-1.5" htmlFor={`${prefixe}-${c}`}>
            <span className="text-sm font-medium">
              {libelle(c)}
              {facultatifs.includes(c) && <span className="font-normal text-muted-foreground"> (facultatif)</span>}
              {renseignes.includes(c) && <span className="font-normal text-muted-foreground"> · déjà renseigné</span>}
            </span>
            <input id={`${prefixe}-${c}`} name={c} className="champ" {...attributs(c)} />
          </label>
        ))}
        {erreur && (
          <p className="text-sm text-danger" role="alert">
            {erreur}
          </p>
        )}
        <div className="boutons">
          <Button type="submit" size="sm" variant="default" disabled={etat === "envoi" || !champs.length}>
            {etat === "envoi" ? <Loader2 className="animate-spin" /> : <Fingerprint />}
            {carte.actions.find((a) => a.action === "coffre_ecrire" || a.action === "coffre")?.libelle || "Enregistrer"}
          </Button>
        </div>
      </form>
    </section>
  );
}

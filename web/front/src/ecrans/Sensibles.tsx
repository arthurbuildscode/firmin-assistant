// Écran Fichiers → « Documents sensibles » (maquette v2, docs/documents-sensibles.md) : chiffrés dans le navigateur,
// visibles par leur titulaire seulement, jamais lus par l'assistant ni envoyés par messagerie. États : verrouillé,
// première fois (phrase de secours à noter), clé absente de l'appareil (phrase), ouvert, vide, connexion de secours.
// Aucun mot technique à l'écran : « protégé par ton empreinte », « phrase de secours ».
import { useEffect, useRef, useState } from "react";
import { FileText, Fingerprint, HeartPulse, IdCard, KeyRound, Loader2, Lock, PawPrint, Plus, ShieldCheck, Trash2, Unlock } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useAssistant, useSession } from "@/lib/session";
import { normaliserPhrase } from "@/lib/sensibles/crypto";
import {
  annulerCreation,
  confirmerCreation,
  deposer,
  deverrouiller,
  effacerErreur,
  fermerDocument,
  initialiser,
  ouvrirAvecPhrase,
  ouvrirDocument,
  preparerDepot,
  supprimer,
  useSensibles,
  verrouiller,
  type DocSensible,
} from "@/store/sensibles";

/** Catégories de la maquette ; les autres catégories du serveur (administratif, banque…) vont dans « Autres ». */
export const CATEGORIES_SENSIBLES = [
  { id: "identite", groupe: "Pièces d’identité", libelle: "Pièce d’identité", nomPropose: "Pièce d’identité", icone: IdCard },
  { id: "sante", groupe: "Carnets de santé", libelle: "Carnet de santé", nomPropose: "Carnet de santé", icone: HeartPulse },
  { id: "animal", groupe: "Carnet de l’animal", libelle: "Carnet de l’animal", nomPropose: "Carnet de l’animal", icone: PawPrint },
  { id: "autre", groupe: "Autres", libelle: "Autre", nomPropose: "Document", icone: FileText },
] as const;

const groupeDe = (categorie: string): string => (CATEGORIES_SENSIBLES.some((c) => c.id === categorie) ? categorie : "autre");
const COULEURS = ["#93c5fd", "#a5b4fc", "#86efac", "#fca5a5", "#fde68a", "#c4b5fd", "#bfdbfe"];

export function tailleLisible(octets: number): string {
  if (octets < 1024 * 1024) return `${Math.max(1, Math.round(octets / 1024))} Ko`;
  return `${(octets / (1024 * 1024)).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} Mo`;
}

const dateCourte = (iso: string): string => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
};

function Illustration({ categorie }: { categorie: string }) {
  const g = groupeDe(categorie);
  if (g === "identite")
    return (
      <span className="vart" aria-hidden>
        <span className="vph" />
        <span className="vl a" />
        <span className="vl b" />
        <span className="vl c" />
        <span className="vmrz">{"IDFRA<<<<<<<<<<<<<<<<<<<<<<<<<<<"}</span>
      </span>
    );
  if (g === "sante" || g === "animal")
    return (
      <span className="vart carnet" aria-hidden>
        <span className="vband" />
        <span className="vcross">{g === "animal" ? <PawPrint /> : "+"}</span>
        <span className="vl a" />
        <span className="vl b" />
        <span className="vl c" />
      </span>
    );
  return (
    <span className="vart doc" aria-hidden>
      <span className="vl a" />
      <span className="vl b" />
      <span className="vl c" />
    </span>
  );
}

function Vignette({ doc, ouvert, couleur, onOuvrir }: { doc: DocSensible; ouvert: boolean; couleur: string; onOuvrir: () => void }) {
  const detail = ouvert ? [doc.date ? `ajouté le ${dateCourte(doc.date)}` : "", tailleLisible(doc.taille)].filter(Boolean).join(" · ") : "Verrouillé";
  return (
    <figure className="vdoc" data-document={doc.id}>
      <button
        type="button"
        className="vimg"
        style={{ "--v": couleur } as React.CSSProperties}
        aria-label={ouvert ? `Ouvrir ${doc.nom}` : `Déverrouiller ${doc.nom}`}
        onClick={onOuvrir}
      >
        <Illustration categorie={doc.categorie} />
        <span className="vlock" aria-hidden>
          <span>
            <Lock className="size-4" />
          </span>
        </span>
      </button>
      <figcaption>
        <div className="text-sm font-semibold leading-snug">{doc.nom}</div>
        <div className="text-xs text-muted-foreground">{detail} · titulaire seul</div>
      </figcaption>
    </figure>
  );
}

function Grille({ documents, ouvert }: { documents: DocSensible[]; ouvert: boolean }) {
  return (
    <div className="mt-4">
      {CATEGORIES_SENSIBLES.map((c, g) => {
        const docs = documents.filter((d) => groupeDe(d.categorie) === c.id);
        if (!docs.length) return null;
        return (
          <div className="vgroupe" key={c.id}>
            <div className="flex items-center justify-between">
              <h3 className="rayon-t">{c.groupe}</h3>
              <span className="num text-sm text-muted-foreground">{docs.length}</span>
            </div>
            <div className="vthumbs">
              {docs.map((d, i) => (
                <Vignette key={d.id} doc={d} ouvert={ouvert} couleur={COULEURS[(2 * g + i) % COULEURS.length]} onOuvrir={() => void ouvrirDocument(d)} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function SaisiePhrase() {
  const occupe = useSensibles((e) => e.occupe);
  const champ = useRef<HTMLTextAreaElement>(null);
  const [saisie, setSaisie] = useState("");
  useEffect(() => champ.current?.focus(), []);
  const envoyer = async () => {
    await ouvrirAvecPhrase(saisie);
    setSaisie("");
  };
  return (
    <form
      className="mt-4 flex flex-col gap-2.5 border-t border-border pt-4"
      data-etat="phrase"
      onSubmit={(e) => {
        e.preventDefault();
        void envoyer();
      }}
    >
      <div className="flex items-start gap-2.5">
        <KeyRound className="mt-0.5 size-4 flex-none text-muted-foreground" />
        <div>
          <div className="font-semibold">Cet appareil n’a pas la clé : utilise ta phrase de secours.</div>
          <p className="text-sm text-muted-foreground">La phrase notée sur papier quand tu as protégé tes premiers documents (11 groupes de 5 caractères).</p>
        </div>
      </div>
      <label className="sr-only" htmlFor="phrase-secours">
        Phrase de secours
      </label>
      <textarea
        id="phrase-secours"
        ref={champ}
        className="champ-phrase"
        value={saisie}
        onChange={(e) => setSaisie(e.target.value)}
        autoComplete="off"
        autoCapitalize="characters"
        autoCorrect="off"
        spellCheck={false}
        placeholder="XXXXX-XXXXX-XXXXX-…"
      />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="default" disabled={occupe || normaliserPhrase(saisie).length < 55}>
          {occupe ? <Loader2 className="animate-spin" /> : <Unlock />}
          Ouvrir
        </Button>
        <Button type="button" variant="ghost" onClick={() => verrouiller("manuel")}>
          Annuler
        </Button>
      </div>
    </form>
  );
}

function DialogueCreation() {
  const { phase, phrase, protection, occupe, erreur } = useSensibles((e) => e);
  const [verif, setVerif] = useState("");
  const ouvert = phase === "creation" && !!phrase;
  useEffect(() => {
    if (!ouvert) setVerif("");
  }, [ouvert]);
  const morceaux = phrase ? phrase.split("-") : [];
  const dernier = morceaux[morceaux.length - 1] ?? "";
  const bon = !!dernier && normaliserPhrase(verif) === dernier;
  return (
    <Dialog open={ouvert} onOpenChange={(o) => !o && annulerCreation()}>
      <DialogContent>
        <DialogTitle>Ta phrase de secours</DialogTitle>
        <DialogDescription>
          {protection === "empreinte"
            ? "Note cette phrase sur papier. Sans elle et sans ta passkey, tes documents sont perdus."
            : "Note cette phrase sur papier. Elle ouvre tes documents sur un autre appareil ; sans elle et sans cet appareil, ils sont perdus."}
        </DialogDescription>
        <ol className="phrase-secours" aria-label="Phrase de secours, 11 groupes de 5 caractères" data-phrase>
          {morceaux.map((g, i) => (
            <li key={i}>{g}</li>
          ))}
        </ol>
        <p className="coffre-etat">
          <ShieldCheck />
          {protection === "empreinte" ? "Ensuite, tes documents seront protégés par ton empreinte." : "Ensuite, tes documents seront protégés sur cet appareil."}
        </p>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (bon) void confirmerCreation();
          }}
        >
          <label className="text-sm font-medium" htmlFor="verif-phrase">
            Pour vérifier, retape le dernier groupe ({morceaux.length})
          </label>
          <input
            id="verif-phrase"
            className="champ-texte font-mono uppercase"
            value={verif}
            onChange={(e) => setVerif(e.target.value)}
            maxLength={7}
            autoComplete="off"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
          {erreur && (
            <p className="text-sm text-danger" role="alert">
              {erreur}
            </p>
          )}
          <div className="mt-1 flex flex-wrap justify-end gap-2">
            <Button type="button" variant="ghost" onClick={annulerCreation}>
              Plus tard
            </Button>
            <Button type="submit" variant="default" disabled={!bon || occupe}>
              {occupe && <Loader2 className="animate-spin" />}
              Je l’ai notée
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DialogueDepot() {
  const { phase, depot, occupe, erreur } = useSensibles((e) => e);
  const [categorie, setCategorie] = useState<string>("identite");
  const [nom, setNom] = useState<string>(CATEGORIES_SENSIBLES[0].nomPropose);
  const [nomModifie, setNomModifie] = useState(false);
  const ouvert = phase === "ouvert" && !!depot;
  useEffect(() => {
    if (ouvert) {
      setCategorie("identite");
      setNom(CATEGORIES_SENSIBLES[0].nomPropose);
      setNomModifie(false);
    }
  }, [ouvert, depot]);
  const choisir = (id: string) => {
    setCategorie(id);
    if (!nomModifie) setNom(CATEGORIES_SENSIBLES.find((c) => c.id === id)?.nomPropose ?? "Document");
  };
  const photo = depot?.type.startsWith("image/") ?? false;
  return (
    <Dialog open={ouvert} onOpenChange={(o) => !o && preparerDepot(null)}>
      <DialogContent>
        <DialogTitle>Ajouter un document sensible</DialogTitle>
        <DialogDescription>
          {photo ? "Photo" : "Document"}
          {depot ? ` · ${tailleLisible(depot.size)}` : ""} · chiffré sur cet appareil avant l’envoi.
        </DialogDescription>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (depot && nom.trim()) void deposer(depot, nom, categorie);
          }}
        >
          <div role="radiogroup" aria-label="Catégorie" className="choix-cat">
            {CATEGORIES_SENSIBLES.map(({ id, libelle, icone: Icone }) => (
              <button key={id} type="button" role="radio" aria-checked={categorie === id} onClick={() => choisir(id)}>
                <Icone />
                {libelle}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-1.5">
            <label className="text-sm font-medium" htmlFor="nom-sensible">
              Nom
            </label>
            <input
              id="nom-sensible"
              className="champ-texte"
              value={nom}
              maxLength={80}
              autoComplete="off"
              onChange={(e) => {
                setNom(e.target.value);
                setNomModifie(true);
              }}
            />
            <p className="text-xs text-muted-foreground">Le nom reste lisible : garde-le neutre (« Passeport », pas son numéro).</p>
          </div>
          {erreur && (
            <p className="text-sm text-danger" role="alert">
              {erreur}
            </p>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => preparerDepot(null)}>
              Annuler
            </Button>
            <Button type="submit" variant="default" disabled={occupe || !nom.trim()}>
              {occupe ? <Loader2 className="animate-spin" /> : <Lock />}
              Chiffrer et ranger
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DialogueApercu() {
  const { apercu, documents, occupe } = useSensibles((e) => e);
  const [confirmer, setConfirmer] = useState(false);
  const [illisible, setIllisible] = useState(false);
  useEffect(() => {
    setConfirmer(false);
    setIllisible(false);
  }, [apercu?.id]);
  const doc = apercu ? documents?.find((d) => d.id === apercu.id) : undefined;
  return (
    <Dialog open={!!apercu} onOpenChange={(o) => !o && fermerDocument()}>
      <DialogContent className="lg:w-[min(860px,calc(100vw-48px))]">
        <DialogTitle>{apercu?.nom}</DialogTitle>
        <DialogDescription>
          {doc ? [doc.date ? `Ajouté le ${dateCourte(doc.date)}` : "", tailleLisible(doc.taille)].filter(Boolean).join(" · ") : ""}
          {" · "}visible par toi seul
        </DialogDescription>
        {apercu && (
          <div className="apercu-sensible" data-apercu={apercu.type}>
            {apercu.type === "application/pdf" ? (
              <iframe src={apercu.url} title={apercu.nom} />
            ) : apercu.type.startsWith("image/") && !illisible ? (
              <img src={apercu.url} alt={apercu.nom} onError={() => setIllisible(true)} />
            ) : (
              <p className="p-6 text-sm text-muted-foreground">Aperçu impossible sur cet appareil.</p>
            )}
          </div>
        )}
        {confirmer ? (
          <div className="flex flex-col gap-2 rounded-lg border border-border p-3" role="alertdialog" aria-label="Confirmer la suppression">
            <p className="text-sm">Supprimer « {apercu?.nom} » ? Il sera effacé pour de bon, sans copie ailleurs.</p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button variant="ghost" onClick={() => setConfirmer(false)}>
                Garder
              </Button>
              <Button variant="destructive" disabled={occupe || !doc} onClick={() => doc && void supprimer(doc)}>
                {occupe ? <Loader2 className="animate-spin" /> : <Trash2 />}
                Supprimer
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap justify-between gap-2">
            <Button variant="ghost" onClick={() => setConfirmer(true)}>
              <Trash2 />
              Supprimer
            </Button>
            <Button variant="outline" onClick={fermerDocument}>
              Fermer
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function SectionSensibles() {
  const e = useSensibles((x) => x);
  const assistant = useAssistant();
  const niveau = useSession().niveau;
  const fichier = useRef<HTMLInputElement>(null);
  useEffect(() => initialiser(), [niveau]);
  const ouvert = e.phase === "ouvert";
  const documents = e.documents ?? [];
  const etatProtection =
    e.protection === "empreinte" ? "Protégé par ton empreinte" : e.protection === "appareil" ? "Protégé sur cet appareil" : "Ouvert avec ta phrase de secours";

  return (
    <section className="section max-w-[720px]" id="documents-sensibles" data-sensibles={e.phase}>
      <div className="section-t">
        <h2 className="flex items-center gap-2">
          Documents sensibles <span className="num text-sm font-normal text-muted-foreground">{documents.length || ""}</span>
        </h2>
        <div className="flex flex-wrap gap-2">
          {ouvert ? (
            <Button variant="outline" onClick={() => verrouiller("manuel")}>
              Verrouiller
            </Button>
          ) : (
            e.phase !== "secours" && (
              <Button variant="outline" disabled={e.occupe || e.phase === "creation"} onClick={() => void deverrouiller()}>
                {e.phase === "ouverture" ? <Loader2 className="animate-spin" /> : <Fingerprint />}
                {e.phase === "ouverture" ? "Vérification…" : "Déverrouiller"}
              </Button>
            )
          )}
        </div>
      </div>
      <div className={`carte large coffre ${ouvert ? "ouvert" : "verrouille"}`}>
        <div className="flex items-start gap-2.5">
          <Lock className="mt-0.5 size-4 flex-none text-muted-foreground" />
          <div className="min-w-0">
            <div className="font-semibold">Chiffrés, visibles par toi seul, jamais envoyés par messagerie</div>
            <p className="max-w-[68ch] text-sm text-muted-foreground">
              Pièces d’identité, carnets de santé, carnet de l’animal. Chiffrés sur ton appareil avant l’envoi : ni {assistant}, ni l’autre adulte, ni le
              serveur ne peuvent les lire.
            </p>
            {ouvert && (
              <p className="coffre-etat mt-2" data-protection={e.protection}>
                <ShieldCheck />
                {etatProtection} · se verrouille après 5 minutes sans activité
              </p>
            )}
            {ouvert && documents.length > 0 && (
              <Button className="mt-3" size="sm" variant="outline" onClick={() => fichier.current?.click()}>
                <Plus />
                Ajouter un document
              </Button>
            )}
          </div>
        </div>

        {e.phase === "secours" && (
          <p className="mt-4 border-t border-border pt-4 text-sm" data-etat="secours">
            Connexion de secours : tes documents sensibles restent fermés. Connecte-toi avec ton empreinte pour les ouvrir.
          </p>
        )}
        {e.phase === "phrase" && <SaisiePhrase />}
        {e.depot && !ouvert && e.phase !== "secours" && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-4 text-sm" data-etat="depot-en-attente">
            <span>Déverrouille pour chiffrer et ranger le document choisi.</span>
            <Button variant="ghost" size="sm" onClick={() => preparerDepot(null)}>
              Annuler
            </Button>
          </div>
        )}
        {e.erreur && !e.depot && e.phase !== "creation" && (
          <p className="mt-3 text-sm text-danger" role="alert" onClick={effacerErreur}>
            {e.erreur}
          </p>
        )}
        {ouvert && documents.length === 0 && (
          <div className="vide mt-4 flex flex-col items-start gap-3 border-t border-border pt-4" data-etat="vide">
            <div>Aucun document pour l’instant. Ajoute une photo ou un PDF : il est chiffré ici, avant de partir.</div>
            <Button variant="default" onClick={() => fichier.current?.click()}>
              <Plus />
              Ajouter un document
            </Button>
          </div>
        )}
        {documents.length > 0 && e.phase !== "secours" && <Grille documents={documents} ouvert={ouvert} />}
      </div>
      <input
        ref={fichier}
        type="file"
        hidden
        accept="image/*,application/pdf,.pdf,.heic"
        data-depot-sensible
        onChange={(ev) => {
          const f = ev.target.files?.[0] ?? null;
          ev.target.value = "";
          if (f) preparerDepot(f);
        }}
      />
      <DialogueCreation />
      <DialogueDepot />
      <DialogueApercu />
    </section>
  );
}


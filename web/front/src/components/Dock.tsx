// Champ de saisie sur une ligne (type Gemini), toujours accessible : « + » (photos, fichiers, enregistrer…), texte,
// micro (dictée → transcription locale sur le serveur), bouton voix live qui devient « Envoyer » dès qu'il y a du texte.
import { useEffect, useRef, useState } from "react";
import { AudioLines, BookOpen, Camera, FileText, Image as IconeImage, Loader2, Lock, MessageCircle, Mic, Plus, SendHorizontal, ShoppingCart, Square, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { InfoBulle } from "@/components/ui/tooltip";
import { lireErreur, messageErreur, requeteRessayee } from "@/lib/api";
import { chrono } from "@/lib/dates";
import { naviguer } from "@/lib/routeur";
import { texteALire } from "@/lib/texte";
import { useAssistant } from "@/lib/session";
import { toast } from "@/lib/toasts";
import { Enregistreur, microDisponible, transcrire } from "@/lib/voix";
import { magasin, useConversation } from "@/store/conversation";
import { preparerDepot } from "@/store/sensibles";
import { ModeVoix } from "./ModeVoix";

const CONTEXTES: Record<string, string> = {
  semaine: "À propos de cette semaine",
  courses: "À propos de cette liste",
  repas: "À propos des repas",
  echeances: "À propos de ces échéances",
  journal: "À propos du journal",
  fichiers: "À propos de ces documents",
  memoire: "À propos de la mémoire",
  reglages: "À propos des réglages",
  routines: "À propos des routines",
};

// Suggestions génériques, sans aucune donnée personnelle (règle : discrètes, facultatives, sous le champ).
const SUGGESTIONS = [
  "Qu’est-ce qu’on mange ce soir ?",
  "Ajoute du lait aux courses",
  "Mon agenda de demain",
  "Quelles échéances arrivent ?",
  "Note au journal : ",
];

async function televerser(fichier: File): Promise<{ id: string; nom: string }> {
  const fd = new FormData();
  fd.append("fichier", fichier, fichier.name || "photo.jpg");
  const r = await requeteRessayee("POST", "/api/fichiers", fd);
  if (!r.ok) throw await lireErreur(r);
  return (await r.json()) as { id: string; nom: string };
}

/**
 * Réponse à une demande faite depuis un écran dédié : une ligne dans le dock, au-dessus du champ (jamais par-dessus
 * l'écran). L'écran, lui, montre ses données ; la conversation complète reste dans le fil.
 */
function useReponseEcran({ ecran, assistant }: { ecran: string; assistant: string }) {
  const dernierEcran = useConversation((e) => e.dernierEcran);
  const echange = useConversation((e) => e.dernierEchange);
  const messages = useConversation((e) => e.messages);
  const [fermee, setFermee] = useState<string | null>(null);
  if (dernierEcran !== ecran || !echange || fermee === echange.assistant) return null;
  const a = messages.find((m) => m.cle === echange.assistant);
  if (!a) return null;
  const texte = a.erreur || texteALire(a.texte).replace(/\s+/g, " ").trim();
  if (!a.enCours && !texte) return null;
  return (
    <div className="reponse-ecran" role="status" aria-live="polite" data-reponse-ecran>
      {a.enCours ? (
        <>
          <Loader2 className="size-3.5 flex-none animate-spin" aria-hidden />
          <span className="min-w-0 flex-1 ellip">{a.attente || `${assistant} s’en occupe…`}</span>
        </>
      ) : (
        <>
          <span className={`min-w-0 flex-1 texte${a.erreur ? " text-danger" : ""}`}>{texte}</span>
          <InfoBulle texte="Voir dans la conversation">
            <button type="button" className="bouton-mini" aria-label="Voir dans la conversation" onClick={() => naviguer("conversation")}>
              <MessageCircle />
            </button>
          </InfoBulle>
          <InfoBulle texte="Masquer">
            <button type="button" className="bouton-mini" aria-label="Masquer la réponse" onClick={() => setFermee(echange.assistant)}>
              <X />
            </button>
          </InfoBulle>
        </>
      )}
    </div>
  );
}

function ReponseEcranOuContexte({ ecran, assistant }: { ecran: string; assistant: string }) {
  const reponse = useReponseEcran({ ecran, assistant });
  if (reponse) return reponse;
  return CONTEXTES[ecran] ? <div className="contexte-ecran">{CONTEXTES[ecran]}</div> : null;
}

export function Dock({ ecran, objet }: { ecran: string; objet?: string }) {
  const assistant = useAssistant();
  const enCours = useConversation((e) => e.echangeEnCours);
  const [texte, setTexte] = useState("");
  const [dictee, setDictee] = useState<"non" | "ecoute" | "transcription">("non");
  const [enreg, setEnreg] = useState<{ debut: number } | null>(null);
  const [secondes, setSecondes] = useState(0);
  const [menu, setMenu] = useState(false);
  const [voix, setVoix] = useState(false);
  const [envoiFichier, setEnvoiFichier] = useState(false);
  const enregistreur = useRef<Enregistreur | null>(null);
  const champ = useRef<HTMLInputElement>(null);
  const dock = useRef<HTMLDivElement>(null);
  const fichierPhotos = useRef<HTMLInputElement>(null);
  const fichierCamera = useRef<HTMLInputElement>(null);
  const fichierDocs = useRef<HTMLInputElement>(null);
  const fichierSensible = useRef<HTMLInputElement>(null);
  const conversation = ecran === "conversation";
  const contexte = { ecran, objet };

  // Hauteur réelle du dock → marge du contenu (rien ne passe jamais sous le champ).
  useEffect(() => {
    const el = dock.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => document.documentElement.style.setProperty("--dock-h", `${el.offsetHeight}px`));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!enreg) return;
    const t = window.setInterval(() => setSecondes(Math.floor((Date.now() - enreg.debut) / 1000)), 500);
    return () => window.clearInterval(t);
  }, [enreg]);

  useEffect(() => () => enregistreur.current?.annuler(), []);

  // Texte prérempli (« Ajoute à l’agenda : ») : si la personne change d’écran sans l’avoir complété, il disparaît
  // (sinon le champ d’un autre écran affichait « Ajoute à l’agenda : » et le bouton Envoyer au lieu du mode voix).
  const prerempli = useRef<string | null>(null);
  useEffect(() => {
    const f = (e: Event) => {
      const t = String((e as CustomEvent).detail || "");
      prerempli.current = t;
      setTexte(t);
      setTimeout(() => champ.current?.focus(), 30);
    };
    const importer = (e: Event) => {
      const quoi = String((e as CustomEvent).detail || "photos");
      (quoi === "documents" ? fichierDocs : quoi === "camera" ? fichierCamera : fichierPhotos).current?.click();
    };
    window.addEventListener("homeos:preremplir", f);
    window.addEventListener("homeos:importer", importer);
    return () => {
      window.removeEventListener("homeos:preremplir", f);
      window.removeEventListener("homeos:importer", importer);
    };
  }, []);

  useEffect(() => {
    setTexte((t) => (prerempli.current !== null && t.trim() === prerempli.current.trim() ? "" : t));
    prerempli.current = null;
  }, [ecran]);

  const envoyer = (t: string, options: { voix?: boolean } = {}) => {
    if (!t.trim()) return;
    void magasin.envoyer(t, { contexte, ...options });
    setTexte("");
  };

  const basculerDictee = async () => {
    if (dictee === "ecoute") {
      enregistreur.current?.arreter();
      return;
    }
    if (!microDisponible()) {
      toast("Micro indisponible sur ce navigateur.");
      return;
    }
    const e = new Enregistreur();
    enregistreur.current = e;
    try {
      const fini = await e.demarrer();
      setDictee("ecoute");
      const audio = await fini;
      setDictee("transcription");
      const t = await transcrire(audio);
      if (t) envoyer(t, { voix: true });
      else toast("Je n’ai rien entendu.");
    } catch (err) {
      toast(err instanceof DOMException ? "Accès au micro refusé." : messageErreur(err));
    } finally {
      setDictee("non");
      enregistreur.current = null;
    }
  };

  const demarrerEnregistrement = async () => {
    setMenu(false);
    if (!microDisponible()) {
      toast("Micro indisponible sur ce navigateur.");
      return;
    }
    const e = new Enregistreur();
    enregistreur.current = e;
    try {
      const fini = await e.demarrer();
      const debut = Date.now();
      setEnreg({ debut });
      setSecondes(0);
      const audio = await fini;
      const duree = chrono(Math.round((Date.now() - debut) / 1000));
      setEnreg(null);
      toast("Je transcris l’enregistrement…", 6000);
      const t = await transcrire(audio);
      if (!t) {
        toast("Je n’ai rien entendu dans l’enregistrement.");
        return;
      }
      void magasin.envoyer(`[enregistrement de ${duree} transcrit]\n${t}\n\nFais-en un compte rendu et propose les actions utiles.`, { contexte, voix: true });
    } catch (err) {
      setEnreg(null);
      toast(err instanceof DOMException ? "Accès au micro refusé." : messageErreur(err));
    } finally {
      enregistreur.current = null;
    }
  };

  const choisirFichier = async (liste: FileList | null) => {
    setMenu(false);
    const fichiers = Array.from(liste || []).slice(0, 5);
    if (!fichiers.length) return;
    setEnvoiFichier(true);
    try {
      const pieces = [];
      for (const f of fichiers) pieces.push(await televerser(f));
      // La photo part tout de suite : le classement est proposé dans la conversation (zéro bac d'attente).
      await magasin.envoyer(texte, { pieces, contexte });
      setTexte("");
    } catch (err) {
      toast(messageErreur(err));
    } finally {
      setEnvoiFichier(false);
      for (const r of [fichierPhotos, fichierCamera, fichierDocs]) if (r.current) r.current.value = "";
    }
  };

  const prefixer = (p: string) => {
    setMenu(false);
    setTexte(p);
    setTimeout(() => champ.current?.focus(), 50);
  };

  return (
    <div className="dock" ref={dock}>
      <div className="dock-int">
        {!conversation && (
          <ReponseEcranOuContexte ecran={ecran} assistant={assistant} />
        )}
        {enreg ? (
          <div className="enregistrement" role="status">
            <span className="rec" />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold">Enregistrement en cours</div>
              <div className="indice text-xs text-muted-foreground">Gardé sur le serveur du foyer le temps de la transcription</div>
            </div>
            <span className="num font-semibold">{chrono(secondes)}</span>
            <Button className="h-10 rounded-full" variant="destructive" onClick={() => enregistreur.current?.arreter()}>
              <Square />
              Arrêter
            </Button>
          </div>
        ) : (
          <form
            className="flex min-w-0 items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              envoyer(texte);
            }}
          >
            <div className="saisie">
              {dictee === "non" && (
                <InfoBulle texte="Photos, fichiers, enregistrer…">
                  <button type="button" className="bouton-rond" aria-label="Plus d’options : photos, fichiers, enregistrer" onClick={() => setMenu(true)}>
                    {envoiFichier ? <Loader2 className="animate-spin" /> : <Plus />}
                  </button>
                </InfoBulle>
              )}
              {dictee === "non" ? (
                <input
                  ref={champ}
                  name="message"
                  value={texte}
                  onChange={(e) => setTexte(e.target.value)}
                  autoComplete="off"
                  enterKeyHint="send"
                  maxLength={8000}
                  placeholder={conversation ? `Écrire à ${assistant}…` : `Demander à ${assistant}…`}
                  aria-label={conversation ? `Message à ${assistant}` : `Demander à ${assistant} ${CONTEXTES[ecran]?.toLowerCase() ?? ""}`}
                />
              ) : (
                <div className="ecoute-en-cours" role="status">
                  {dictee === "ecoute" ? (
                    <>
                      <span className="onde" aria-hidden>
                        <i />
                        <i />
                        <i />
                        <i />
                        <i />
                      </span>
                      J’écoute…
                      <span className="indice text-xs font-normal text-muted-foreground">touche le micro pour envoyer</span>
                    </>
                  ) : (
                    <>
                      <Loader2 className="size-4 animate-spin" />
                      Je transcris…
                    </>
                  )}
                </div>
              )}
              <InfoBulle texte={dictee === "ecoute" ? "Terminer et envoyer" : "Dicter un message"}>
                <button
                  type="button"
                  className={`bouton-rond${dictee === "ecoute" ? " ecoute" : ""}`}
                  aria-pressed={dictee === "ecoute"}
                  aria-label={dictee === "ecoute" ? "Terminer et envoyer le message vocal" : "Dicter un message"}
                  disabled={dictee === "transcription"}
                  onClick={basculerDictee}
                >
                  {dictee === "ecoute" ? <Square /> : <Mic />}
                </button>
              </InfoBulle>
              {dictee === "non" &&
                (texte.trim() ? (
                  <button type="submit" className="bouton-rond plein" aria-label="Envoyer" aria-disabled={enCours} data-envoyer>
                    <SendHorizontal />
                  </button>
                ) : (
                  <InfoBulle texte={`Mode voix : parler avec ${assistant} à voix haute`}>
                    <button type="button" className="bouton-rond plein" aria-label={`Mode voix : parler avec ${assistant} à voix haute`} onClick={() => setVoix(true)}>
                      <AudioLines />
                    </button>
                  </InfoBulle>
                ))}
            </div>
          </form>
        )}
        {conversation && !enreg && (
          <div className="suggestions" aria-label="Suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} type="button" className="puce" onClick={() => (s.endsWith(": ") ? prefixer(s) : envoyer(s))}>
                {s.replace(/ : $/, "")}
              </button>
            ))}
          </div>
        )}
      </div>

      <input ref={fichierPhotos} type="file" accept="image/*" multiple hidden onChange={(e) => choisirFichier(e.target.files)} />
      <input ref={fichierCamera} type="file" accept="image/*" capture="environment" hidden onChange={(e) => choisirFichier(e.target.files)} />
      <input ref={fichierDocs} type="file" accept=".pdf,.jpg,.jpeg,.png,.heic,application/pdf,image/*" hidden onChange={(e) => choisirFichier(e.target.files)} />
      <input
        ref={fichierSensible}
        type="file"
        accept="image/*,application/pdf,.pdf,.heic"
        hidden
        data-depot-sensible-menu
        onChange={(e) => {
          // Document sensible : jamais envoyé à la conversation ; chiffré dans l'écran Fichiers après déverrouillage.
          const f = e.target.files?.[0] ?? null;
          e.target.value = "";
          if (!f) return;
          setMenu(false);
          preparerDepot(f);
          naviguer("fichiers");
        }}
      />

      <Dialog open={menu} onOpenChange={setMenu}>
        <DialogContent aria-describedby={undefined}>
          <DialogTitle>Ajouter</DialogTitle>
          <div className="tuiles-plus">
            <button className="tuile-plus" onClick={() => fichierPhotos.current?.click()}>
              <IconeImage className="size-6" />
              Photos
            </button>
            <button className="tuile-plus" onClick={() => fichierCamera.current?.click()}>
              <Camera className="size-6" />
              Appareil photo
            </button>
            <button className="tuile-plus" onClick={() => fichierDocs.current?.click()}>
              <FileText className="size-6" />
              Fichiers
            </button>
            <button className="tuile-plus" onClick={demarrerEnregistrement} title="Enregistrer une conversation longue (réunion, rendez-vous) à analyser ensuite">
              <span className="point-rouge" aria-hidden />
              Enregistrer
            </button>
          </div>
          <div className="liste">
            <button className="lien-plus min-h-12" onClick={() => { setMenu(false); setVoix(true); }}>
              <AudioLines className="size-[18px]" />
              <span className="text-base">Mode voix</span>
            </button>
            <button className="lien-plus min-h-12" onClick={() => prefixer("Ajoute aux courses : ")}>
              <ShoppingCart className="size-[18px]" />
              <span className="text-base">Ajouter aux courses</span>
            </button>
            <button className="lien-plus min-h-12" onClick={() => prefixer("Note au journal : ")}>
              <BookOpen className="size-[18px]" />
              <span className="text-base">Note au journal</span>
            </button>
            <button className="lien-plus min-h-12" onClick={() => fichierSensible.current?.click()}>
              <Lock className="size-[18px]" />
              <span className="text-base">Document sensible</span>
              <span className="ml-auto text-xs text-muted-foreground">chiffré, jamais lu par {assistant}</span>
            </button>
          </div>
        </DialogContent>
      </Dialog>
      {voix && <ModeVoix ecran={ecran} onFermer={() => setVoix(false)} />}
    </div>
  );
}

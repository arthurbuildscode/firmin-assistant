// Écrans « La famille » : journal, fichiers (rangés, à classer, sensibles), mémoire. Sources : les écrans sans modèle
// (`web/ecrans.py` : `GET /api/journal`, `/api/fichiers`, `/api/fichiers/entrants`, `/api/memoire`), sinon les cartes
// du fil. Documents sensibles : chiffrés dans le navigateur (./Sensibles.tsx, docs/documents-sensibles.md).
import { useEffect, useRef, useState } from "react";
import { BookOpen, Camera, ImagePlus, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Carte } from "@/components/cartes/Carte";
import { api, ErreurApi, lireErreur, messageErreur, requeteRessayee } from "@/lib/api";
import { dateLocale, nomJourCourt } from "@/lib/dates";
import { preremplirDock } from "@/lib/dock";
import { listeDe, useDonnees } from "@/lib/donnees";
import { naviguer } from "@/lib/routeur";
import { useAssistant, useSession } from "@/lib/session";
import { TexteRiche } from "@/lib/texte";
import { toast } from "@/lib/toasts";
import { cartesDuType, magasin, useConversation } from "@/store/conversation";
import { BoutonActualiser, DateColonne, TitrePage, Vide } from "./commun";
import { SectionSensibles } from "./Sensibles";

const importer = (quoi: "photos" | "camera" | "documents") => window.dispatchEvent(new CustomEvent("homeos:importer", { detail: quoi }));
const MOIS_LONGS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"];
const nomMois = (m: string) => {
  const [a, n] = m.split("-").map(Number);
  return n ? `${MOIS_LONGS[n - 1].replace(/^./, (c) => c.toUpperCase())} ${a}` : m;
};

// --- Journal --------------------------------------------------------------------------------------------------------

type Entree = { quand?: string | null; qui?: string | null; texte?: string; url?: string };
type Journal = { mois: string; entrees: Entree[]; mois_disponibles: string[] };

const validerJournal = (brut: unknown): Journal | null => {
  const entrees = listeDe<Entree>(brut, "entrees");
  if (!entrees) return null;
  const o = brut as Record<string, unknown>;
  return { entrees, mois: String(o.mois || ""), mois_disponibles: listeDe<string>(o, "mois_disponibles") ?? [] };
};
const urlServie = (u?: string) => typeof u === "string" && /^\/api\//.test(u);

export function EcranJournal() {
  const messages = useConversation((e) => e.messages);
  const [mois, setMois] = useState<string | null>(null);
  const direct = useDonnees("journal", validerJournal, mois ? `/api/journal?mois=${mois}` : "/api/journal");
  const journaux = cartesDuType(messages, ["journal", "compte_rendu", "routine_resultat"], 10);
  const contexte = { ecran: "journal", objet: "journal du foyer" };
  const j = direct.statut === "ok" ? direct.donnees! : null;
  const [note, setNote] = useState<string | null>(null);
  const [photo, setPhoto] = useState<{ fichier: File; legende: string } | null>(null);
  const [envoi, setEnvoi] = useState(false);
  const choix = useRef<HTMLInputElement>(null);
  const photos = (j?.entrees ?? []).filter((e) => urlServie(e.url));
  const lignes = [...(j?.entrees ?? [])].filter((e) => !e.url).reverse(); // la plus récente d'abord (maquette)
  const moisListe = j ? [...new Set([j.mois, ...j.mois_disponibles])].filter(Boolean).slice(0, 3) : [];

  const ecrireNote = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!note?.trim()) return;
    setEnvoi(true);
    try {
      await api("POST", "/api/journal", { texte: note.trim() });
      setNote(null);
      void direct.recharger();
    } catch (err) {
      toast(messageErreur(err));
    } finally {
      setEnvoi(false);
    }
  };

  const ajouterPhoto = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!photo) return;
    setEnvoi(true);
    try {
      const fd = new FormData();
      fd.append("fichier", photo.fichier, photo.fichier.name || "photo.jpg");
      const r = await requeteRessayee("POST", "/api/fichiers", fd);
      if (!r.ok) throw await lireErreur(r);
      const { id } = (await r.json()) as { id: string };
      await api("POST", "/api/journal", { texte: photo.legende.trim() || "Photo", piece: id });
      setPhoto(null);
      void direct.recharger();
    } catch (err) {
      toast(messageErreur(err));
    } finally {
      setEnvoi(false);
    }
  };

  return (
    <>
      <TitrePage titre="Journal" sous="Souvenirs de la famille, écrits par message, vocal ou photo" />
      <div className="mb-6 flex flex-wrap gap-2">
        <Button variant="default" onClick={() => (j ? choix.current?.click() : importer("photos"))}>
          <ImagePlus />
          Ajouter une photo
        </Button>
        <Button variant="ghost" onClick={() => (j ? setNote("") : preremplirDock("Note au journal : "))}>
          <BookOpen />
          Écrire une note
        </Button>
        <input
          ref={choix}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) setPhoto({ fichier: f, legende: "" });
            e.target.value = "";
          }}
        />
      </div>
      {note !== null && (
        <form className="mb-6 flex max-w-[720px] gap-2" onSubmit={ecrireNote}>
          <input className="champ min-w-0 flex-1" autoFocus value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="Ce qui s’est passé aujourd’hui…" aria-label="Note au journal" />
          <Button type="submit" disabled={envoi || !note.trim()}>
            Ajouter
          </Button>
          <Button type="button" variant="ghost" onClick={() => setNote(null)}>
            Annuler
          </Button>
        </form>
      )}
      {photo && (
        <form className="mb-6 flex max-w-[720px] flex-wrap gap-2" onSubmit={ajouterPhoto}>
          <span className="w-full text-sm text-muted-foreground">{photo.fichier.name}</span>
          <input className="champ min-w-0 flex-1" autoFocus value={photo.legende} maxLength={500} onChange={(e) => setPhoto({ ...photo, legende: e.target.value })} placeholder="Une légende, ex. « Balade au lac »" aria-label="Légende de la photo" />
          <Button type="submit" disabled={envoi}>
            Ajouter au journal
          </Button>
          <Button type="button" variant="ghost" onClick={() => setPhoto(null)}>
            Annuler
          </Button>
        </form>
      )}
      {j ? (
        <div className="flex max-w-[720px] flex-col gap-8">
          {moisListe.length > 1 && (
            <div className="seg self-start" role="group" aria-label="Mois">
              {moisListe.map((m) => (
                <button key={m} aria-pressed={m === j.mois} onClick={() => setMois(m)}>
                  {nomMois(m)}
                </button>
              ))}
            </div>
          )}
          {photos.length > 0 && (
            <section>
              <div className="section-t">
                <h2>
                  Photos <span className="num text-sm font-normal text-muted-foreground">{photos.length}</span>
                </h2>
              </div>
              <div className="coffre-grille">
                {photos.map((p, i) => (
                  <figure key={i} className="min-w-0">
                    <img src={p.url} alt={p.texte || "Photo du journal"} className="aspect-[4/3] w-full rounded-lg border object-cover" loading="lazy" />
                    {p.texte && <figcaption className="mt-1.5 text-sm">{p.texte}</figcaption>}
                    {p.quand && <div className="text-xs text-muted-foreground">{dateLocale(p.quand.slice(0, 10))?.toLocaleDateString("fr-FR", { day: "numeric", month: "long" })}</div>}
                  </figure>
                ))}
              </div>
            </section>
          )}
          <section>
            <div className="section-t">
              <h2>
                Entrées <span className="num text-sm font-normal text-muted-foreground">{lignes.length}</span>
              </h2>
            </div>
            {lignes.length === 0 ? (
              <Vide>Une note dictée, une photo, un compte rendu : tout ce qui compte pour le foyer s’écrit ici en parlant.</Vide>
            ) : (
              <div className="liste">
                {lignes.map((e, i) => {
                  const d = e.quand ? dateLocale(e.quand.slice(0, 10)) : null;
                  return (
                    <div className="ligne-ech" key={`${e.quand}-${i}`}>
                      <DateColonne jour={d?.getDate() ?? ""} sous={d ? nomJourCourt(d) : undefined} />
                      <div className="min-w-0 flex-1">
                        <div className="ligne-t">{e.texte}</div>
                        {e.qui && <div className="ligne-s text-xs">{e.qui}</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </div>
      ) : direct.statut === "chargement" ? null : journaux.length ? (
        <div className="flex flex-col gap-4">
          {journaux.map((c) => (
            <Carte key={c.id} carte={c} contexte={{ ecran: "journal" }} />
          ))}
        </div>
      ) : (
        <Vide action={<BoutonActualiser texte="Montre-moi les dernières entrées du journal." contexte={contexte} libelle="Afficher le journal" />}>
          Une note dictée, une photo, un compte rendu : tout ce qui compte pour le foyer s’écrit ici en parlant.
        </Vide>
      )}
    </>
  );
}

// --- Fichiers -------------------------------------------------------------------------------------------------------

type Fichier = { date?: string; categorie?: string; nom?: string; description?: string; existe?: boolean; url?: string };
type Fichiers = { categories: string[]; fichiers: Fichier[] };
type Entrant = { nom?: string; taille?: string | number; age?: string; url?: string };

const CATEGORIES: Record<string, string> = {
  voiture: "Voiture",
  animaux: "Animaux",
  enfants: "Enfants",
  maison: "Maison",
  sante: "Santé",
  administratif: "Administratif",
  factures: "Factures",
  photos: "Photos",
  autre: "Autre",
};
const libelleCategorie = (c?: string) => CATEGORIES[String(c || "")] ?? String(c || "Autre").replace(/^./, (x) => x.toUpperCase());

const validerFichiers = (brut: unknown): Fichiers | null => {
  const fichiers = listeDe<Fichier>(brut, "fichiers");
  if (!fichiers) return null;
  return { fichiers, categories: listeDe<string>(brut as Record<string, unknown>, "categories") ?? [] };
};
const validerEntrants = (brut: unknown) => listeDe<Entrant>(brut, "entrants");

function detailFichier(f: Fichier): string {
  const ext = (f.nom || "").split(".").pop()?.toUpperCase();
  const d = f.date ? dateLocale(f.date) : null;
  return [ext, d ? `ajouté le ${d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })}` : ""].filter(Boolean).join(" · ");
}

export function EcranFichiers() {
  const assistant = useAssistant();
  const messages = useConversation((e) => e.messages);
  const direct = useDonnees("fichiers", validerFichiers);
  const entrants = useDonnees("fichiers", validerEntrants, "/api/fichiers/entrants");
  const ranges = cartesDuType(messages, ["fichier_classe", "fichier_recu"], 30);
  const contexte = { ecran: "fichiers", objet: "documents du foyer" };
  const f = direct.statut === "ok" ? direct.donnees! : null;
  const groupes = new Map<string, Fichier[]>();
  for (const x of f?.fichiers ?? []) {
    const c = String(x.categorie || "autre");
    if (!groupes.has(c)) groupes.set(c, []);
    groupes.get(c)!.push(x);
  }
  const ordre = [...groupes.keys()].sort((a, b) => (f?.categories.indexOf(a) ?? 0) - (f?.categories.indexOf(b) ?? 0));
  const aClasser = entrants.statut === "ok" ? entrants.donnees! : [];

  return (
    <>
      <TitrePage titre="Fichiers" sous={`Documents rangés par ${assistant}, depuis la conversation, Telegram ou l’e-mail`} />
      <div className="mb-6 flex flex-wrap gap-2">
        <Button variant="default" onClick={() => importer("camera")}>
          <Camera />
          Importer une photo
        </Button>
      </div>
      <section className="max-w-[720px]">
        <div className="section-t">
          <h2>Documents classés</h2>
        </div>
        {f ? (
          f.fichiers.length === 0 ? (
            <Vide>Prends une photo ou envoie un PDF depuis le « + » : le classement est proposé dans la conversation.</Vide>
          ) : (
            <div className="flex flex-col gap-6">
              {ordre.map((c) => (
                <div key={c}>
                  <div className="mb-0.5 flex items-center justify-between">
                    <h3 className="rayon-t">{libelleCategorie(c)}</h3>
                    <span className="num text-sm text-muted-foreground">{groupes.get(c)!.length}</span>
                  </div>
                  <div className="liste">
                    {groupes.get(c)!.map((x, k) => (
                      <a key={`${x.nom}-${k}`} className="block py-3 no-underline" href={x.url} target="_blank" rel="noopener" aria-disabled={!x.url}>
                        <div className="ligne-t">{x.description || x.nom}</div>
                        <div className="ligne-s text-xs">{detailFichier(x)}</div>
                      </a>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )
        ) : direct.statut === "chargement" ? null : ranges.length ? (
          <div className="flex flex-col gap-3">
            {ranges.map((c) => (
              <Carte key={c.id} carte={c} contexte={{ ecran: "fichiers" }} />
            ))}
          </div>
        ) : (
          <Vide action={<BoutonActualiser texte="Liste les documents rangés récemment." contexte={contexte} libelle="Afficher les documents" />}>
            Prends une photo ou envoie un PDF depuis le « + » : le classement est proposé dans la conversation.
          </Vide>
        )}
      </section>
      <SectionSensibles />
      {aClasser.length > 0 && (
        <details className="mt-6 max-w-[720px] text-sm text-muted-foreground">
          <summary className="cursor-pointer">À classer ({aClasser.length}) · classé automatiquement dans la conversation quand c’est possible</summary>
          <div className="liste mt-2">
            {aClasser.map((x, i) => (
              <a key={`${x.nom}-${i}`} className="block py-2 text-foreground no-underline" href={x.url} target="_blank" rel="noopener">
                {x.nom}
                {x.age && <span className="text-muted-foreground"> · {x.age}</span>}
              </a>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

// --- Mémoire --------------------------------------------------------------------------------------------------------

type FicheResume = { chemin: string; titre?: string; niveau?: string; maj?: string };
type Memoire = { fiches: FicheResume[]; modifiable: boolean };
type FicheLue = { chemin: string; contenu: string; version: string; modifiable?: boolean };

const validerMemoire = (brut: unknown): Memoire | null => {
  const fiches = listeDe<FicheResume>(brut, "fiches");
  if (!fiches) return null;
  return { fiches: fiches.filter((f) => typeof f.chemin === "string"), modifiable: !!(brut as { modifiable?: unknown }).modifiable };
};

const DOSSIERS: Record<string, string> = { foyer: "Famille", personnes: "Personnes", alimentation: "Repas", animaux: "Animaux", voitures: "Voitures" };

function groupeDe(f: FicheResume, prenom: string): string {
  if (f.niveau === "personnel") return prenom || "Moi";
  return DOSSIERS[f.chemin.split("/")[0]] ?? "Famille";
}

function quandModifie(iso?: string): string {
  const d = iso ? new Date(iso) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit", year: "numeric" }) : "";
}

function FicheMemoire({ resume, groupe, modifiable }: { resume: FicheResume; groupe: string; modifiable: boolean }) {
  const [fiche, setFiche] = useState<FicheLue | null>(null);
  const [brouillon, setBrouillon] = useState<string | null>(null);
  const [erreur, setErreur] = useState<string | null>(null);
  const [envoi, setEnvoi] = useState(false);
  // Monté une fois par fiche (clé = chemin) : lecture de son contenu et de sa version.
  useEffect(() => {
    let actif = true;
    api<FicheLue>("GET", `/api/memoire/fiche?chemin=${encodeURIComponent(resume.chemin)}`)
      .then((f) => actif && setFiche(f))
      .catch((e) => actif && setErreur(messageErreur(e)));
    return () => {
      actif = false;
    };
  }, [resume.chemin]);
  const enregistrer = async () => {
    if (!fiche || brouillon === null) return;
    setEnvoi(true);
    try {
      const r = await api<{ version: string }>("PUT", "/api/memoire/fiche", { chemin: fiche.chemin, contenu: brouillon, version: fiche.version });
      setFiche({ ...fiche, contenu: brouillon, version: r.version });
      setBrouillon(null);
      toast("Fiche enregistrée.");
    } catch (e) {
      if (e instanceof ErreurApi && e.statut === 409) {
        // Modifiée entre-temps (autre adulte, assistant) : on relit et on garde le brouillon pour fusionner.
        const f = await api<FicheLue>("GET", `/api/memoire/fiche?chemin=${encodeURIComponent(fiche.chemin)}`).catch(() => null);
        if (f) setFiche(f);
        toast("La fiche a changé entre-temps : relis-la, ton texte est gardé, puis enregistre à nouveau.", 7000);
      } else toast(messageErreur(e));
    } finally {
      setEnvoi(false);
    }
  };
  return (
    <section className="carte large" aria-label={`Fiche ${resume.titre}`}>
      <div>
        <h3 className="carte-titre">
          <span className="font-normal text-muted-foreground">{groupe} › </span>
          {resume.titre}
        </h3>
        {resume.maj && <p className="text-xs text-muted-foreground">Modifié le {quandModifie(resume.maj)}</p>}
      </div>
      {erreur && <p className="text-sm text-danger">{erreur}</p>}
      {!fiche && !erreur && <p className="text-sm text-muted-foreground">…</p>}
      {fiche && brouillon === null && (
        <div className="text-sm">
          <TexteRiche texte={fiche.contenu.replace(/^# .*\n+/, "")} />
        </div>
      )}
      {fiche && brouillon !== null && (
        <textarea className="champ zone-fiche" value={brouillon} onChange={(e) => setBrouillon(e.target.value)} aria-label={`Contenu de la fiche ${resume.titre}`} rows={10} />
      )}
      <div className="boutons">
        {brouillon === null ? (
          <>
            {modifiable && fiche?.modifiable !== false && (
              <Button size="sm" variant="outline" disabled={!fiche} onClick={() => setBrouillon(fiche?.contenu ?? "")}>
                <Pencil />
                Modifier
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={() => preremplirDock(`Dans la fiche « ${resume.titre} », corrige : `)}>
              Corriger en parlant
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="ghost" onClick={() => setBrouillon(null)}>
              Annuler
            </Button>
            <Button size="sm" disabled={envoi || brouillon === fiche?.contenu} onClick={() => void enregistrer()}>
              Enregistrer
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

export function EcranMemoire() {
  const assistant = useAssistant();
  const session = useSession();
  const direct = useDonnees("memoire", validerMemoire);
  const contexte = { ecran: "memoire", objet: "mémoire" };
  const m = direct.statut === "ok" ? direct.donnees! : null;
  const [choisie, setChoisie] = useState<string | null>(null);
  const prenom = session.personne?.prenom ?? "";
  const groupes = new Map<string, FicheResume[]>();
  for (const f of m?.fiches ?? []) {
    const g = groupeDe(f, prenom);
    if (!groupes.has(g)) groupes.set(g, []);
    groupes.get(g)!.push(f);
  }
  const courante = (m?.fiches ?? []).find((f) => f.chemin === choisie) ?? m?.fiches[0];
  const demandes = [
    ["Ce que tu sais de moi", "Résume ce que tu sais de moi, en puces courtes."],
    ["Ce que tu sais du foyer", "Résume ce que tu sais du foyer (habitudes, préférences, lieux), en puces courtes."],
    ["Ce que tu as appris récemment", "Qu’as-tu appris de nouveau sur nous ces derniers jours ?"],
  ];
  return (
    <>
      <TitrePage
        titre="Mémoire de la famille"
        sous={`Ce que ${assistant} sait de la famille et de toi, rangé par personne et par sujet. Les événements privés de chacun n’y sont jamais recopiés ; ta fiche personnelle n’est visible que par toi.`}
      />
      {m ? (
        m.fiches.length === 0 ? (
          <Vide>Rien encore : ce que tu dis à {assistant} d’important pour le foyer s’écrit ici, une fiche par sujet et par personne.</Vide>
        ) : (
          <div className="flex max-w-[720px] flex-col gap-6">
            {[...groupes].map(([g, fiches]) => (
              <section key={g}>
                <h2 className="rayon-t mb-2">{g}</h2>
                <div className="flex flex-wrap gap-2">
                  {fiches.map((f) => (
                    <button key={f.chemin} type="button" className="puce puce-fiche" aria-pressed={courante?.chemin === f.chemin} onClick={() => setChoisie(f.chemin)}>
                      {f.titre || f.chemin}
                    </button>
                  ))}
                </div>
              </section>
            ))}
            {courante && <FicheMemoire key={courante.chemin} resume={courante} groupe={groupeDe(courante, prenom)} modifiable={m.modifiable} />}
          </div>
        )
      ) : direct.statut === "chargement" ? null : (
        <>
          <div className="liste max-w-[720px] rounded-lg border px-4">
            {demandes.map(([libelle, texte]) => (
              <button
                key={libelle}
                className="lien-plus"
                onClick={() => {
                  // La réponse est un texte à lire : elle s'affiche dans la conversation.
                  naviguer("conversation");
                  void magasin.envoyer(texte, { contexte });
                }}
              >
                <span className="ligne-t flex-1">{libelle}</span>
              </button>
            ))}
          </div>
          <p className="mt-4 max-w-[68ch] text-sm text-muted-foreground">
            Les fiches personnelles ne sont visibles que par leur auteur ; les fiches du foyer par les deux adultes.
          </p>
        </>
      )}
    </>
  );
}

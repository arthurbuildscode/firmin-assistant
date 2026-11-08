// Documents sensibles (docs/documents-sensibles.md, CONTRACT §12 et §15) : déverrouillage, trousseau, dépôt chiffré,
// ouverture en mémoire, suppression, verrouillage automatique. Tout le chiffrement a lieu ici, dans le navigateur.
//
// En mémoire seulement (jamais dans localStorage, sessionStorage ni IndexedDB) : la clé maîtresse (CryptoKey non
// extractible), la sortie PRF d'une passkey le temps d'en dériver une clé, la phrase de secours le temps de la noter,
// le document déchiffré (URL `blob:` révoquée à la fermeture). localStorage ne garde que les sels des enveloppes
// (publics, aussi sur le serveur) pour demander la bonne sortie PRF dès la première empreinte.
import { useSyncExternalStore } from "react";
import { api, confirmerIdentite, confirmerIdentitePrf, ErreurApi, lireErreur, messageErreur, oublierStepUp, quandDeconnecte, requeteRessayee } from "@/lib/api";
import { sessionCourante } from "@/lib/session";
import { lire, ecrire } from "@/lib/stockage";
import { toast } from "@/lib/toasts";
import { ecrireClesAppareil, lireClesAppareil } from "@/lib/sensibles/appareil";
import {
  CleInvalide,
  DocumentInvalide,
  PhraseInvalide,
  aleatoire,
  chiffrerDocument,
  cleDepuisPhrase,
  dechiffrerDocument,
  depuisB64u,
  depuisHex,
  deriverKek,
  egaux,
  envelopperAppareil,
  envelopperPrf,
  genererKekAppareil,
  importerCle,
  kidDuBlob,
  ouvrirEnveloppeAppareil,
  ouvrirEnveloppePrf,
  phraseDepuisCle,
  versHex,
  type EnveloppePrf,
  type Octets,
} from "@/lib/sensibles/crypto";

export const DELAI_VERROUILLAGE_MS = 5 * 60_000;
export const TAILLE_MAX = 20 * 1024 * 1024;
const EVENEMENT_MAJ = "homeos:maj";

export interface DocSensible {
  id: string;
  nom: string;
  categorie: string;
  date: string;
  taille: number;
  cle: string;
}

interface Trousseau {
  version: 1;
  cle: string;
  enveloppes: EnveloppePrf[];
  maj?: string;
}

/**
 * verrouille : état de repos ; ouverture : empreinte demandée ; creation : premier usage, phrase de secours à noter ;
 * phrase : cet appareil n'a pas la clé ; ouvert : déverrouillé ; secours : connexion par code (lecture refusée).
 */
export type Phase = "verrouille" | "ouverture" | "creation" | "phrase" | "ouvert" | "secours";
export type Protection = "empreinte" | "appareil" | "phrase";

export interface Apercu {
  id: string;
  nom: string;
  type: string;
  url: string;
  nomOrigine: string;
}

export interface EtatSensibles {
  phase: Phase;
  /** Métadonnées (libellé, catégorie, date, taille) : gardées en mémoire après le verrouillage, jamais le contenu. */
  documents: DocSensible[] | null;
  protection: Protection | null;
  /** Phrase de secours affichée une seule fois (création), effacée dès la confirmation ou l'abandon. */
  phrase: string | null;
  erreur: string | null;
  occupe: boolean;
  apercu: Apercu | null;
  /** Fichier choisi (menu « + » ou écran) en attente du déverrouillage, puis du libellé et de la catégorie. */
  depot: File | null;
}

const ETAT_INITIAL: EtatSensibles = { phase: "verrouille", documents: null, protection: null, phrase: null, erreur: null, occupe: false, apercu: null, depot: null };

let etat: EtatSensibles = ETAT_INITIAL;
const ecouteurs = new Set<() => void>();

// --- secrets en mémoire (hors de l'état React) --------------------------------------------------------------------
let cle: CryptoKey | null = null;
let kidCourant: string | null = null;
let trousseau: Trousseau | null = null;
/** Sortie PRF d'une passkey sans enveloppe : sert à lui en créer une dès que la clé est retrouvée autrement. */
type SortiePrf = { credentialId: string; sel: Octets; sortie: Octets };
let prfEnAttente: SortiePrf | null = null;
let creation: { cmd: Octets; kid: Octets; prf: SortiePrf | null } | null = null;
let minuterie: number | null = null;
let documentAOuvrir: string | null = null;

function definir(partiel: Partial<EtatSensibles>): void {
  etat = { ...etat, ...partiel };
  ecouteurs.forEach((f) => f());
}

export function useSensibles<T>(selecteur: (e: EtatSensibles) => T): T {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => selecteur(etat),
  );
}

const linuxUser = (): string => sessionCourante().personne?.linux_user ?? "";
const enSecours = (): boolean => sessionCourante().niveau === "secours";

// --- sels des enveloppes PRF (publics) ----------------------------------------------------------------------------
type Sel = { credential_id: string; sel: string };
const cleSels = (u: string) => `homeos-sensibles:sels:${u}`;
const lireSels = (u: string): Sel[] => {
  const v = lire<unknown>(cleSels(u), []);
  return Array.isArray(v) ? (v.filter((x) => x && typeof x.credential_id === "string" && typeof x.sel === "string") as Sel[]) : [];
};
const ecrireSels = (u: string, enveloppes: EnveloppePrf[]) => ecrire(cleSels(u), enveloppes.map((e) => ({ credential_id: e.credential_id, sel: e.sel })));

function effacerOctets(...o: (Uint8Array | null | undefined)[]): void {
  for (const x of o) x?.fill(0);
}

/** Identifiant de clé attendu : celui du trousseau, sinon le plus fréquent parmi les documents. */
function kidAttendu(documents: DocSensible[]): string | null {
  if (trousseau?.cle) return trousseau.cle;
  const n = new Map<string, number>();
  for (const d of documents) if (d.cle) n.set(d.cle, (n.get(d.cle) ?? 0) + 1);
  return [...n].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

// --- verrouillage automatique -------------------------------------------------------------------------------------
const ACTIVITE = ["pointerdown", "keydown", "touchstart", "wheel"] as const;

function relancerMinuterie(): void {
  if (minuterie !== null) window.clearTimeout(minuterie);
  minuterie = window.setTimeout(() => verrouiller("inactivite"), DELAI_VERROUILLAGE_MS);
}

function surVisibilite(): void {
  if (document.visibilityState === "hidden") verrouiller("arriere-plan");
}

function surveiller(actif: boolean): void {
  if (typeof window === "undefined") return;
  if (actif) {
    for (const e of ACTIVITE) window.addEventListener(e, relancerMinuterie, { passive: true });
    document.addEventListener("visibilitychange", surVisibilite);
    window.addEventListener("pagehide", surPageCachee);
    relancerMinuterie();
    return;
  }
  for (const e of ACTIVITE) window.removeEventListener(e, relancerMinuterie);
  document.removeEventListener("visibilitychange", surVisibilite);
  window.removeEventListener("pagehide", surPageCachee);
  if (minuterie !== null) {
    window.clearTimeout(minuterie);
    minuterie = null;
  }
}

function surPageCachee(): void {
  verrouiller("arriere-plan");
}

function fermerApercu(): void {
  if (etat.apercu) URL.revokeObjectURL(etat.apercu.url);
}

/** Oublie la clé et tout ce qui a été déchiffré ; les métadonnées de la liste restent affichées, verrouillées. */
export function verrouiller(raison?: "inactivite" | "arriere-plan" | "manuel" | "deconnexion"): void {
  const etaitOuvert = etat.phase === "ouvert" || etat.phase === "creation";
  surveiller(false);
  fermerApercu();
  cle = null;
  kidCourant = null;
  if (prfEnAttente) effacerOctets(prfEnAttente.sortie);
  prfEnAttente = null;
  if (creation) effacerOctets(creation.cmd, creation.prf?.sortie);
  creation = null;
  documentAOuvrir = null;
  definir({
    phase: enSecours() ? "secours" : "verrouille",
    phrase: null,
    apercu: null,
    protection: null,
    occupe: false,
    erreur: null,
    ...(raison === "deconnexion" ? { documents: null, depot: null } : {}),
  });
  if (etaitOuvert && raison === "inactivite") toast("Documents sensibles verrouillés après 5 minutes sans activité.");
}

function ouvrir(nouvelleCle: CryptoKey, kid: string, protection: Protection, documents: DocSensible[]): void {
  cle = nouvelleCle;
  kidCourant = kid;
  surveiller(true);
  definir({ phase: "ouvert", protection, documents, erreur: null, occupe: false, phrase: null });
  const id = documentAOuvrir;
  documentAOuvrir = null;
  const doc = id ? documents.find((d) => d.id === id) : null;
  if (doc) void ouvrirDocument(doc);
}

// --- réseau ---------------------------------------------------------------------------------------------------------

async function lireListe(): Promise<DocSensible[]> {
  const r = await api<{ documents?: DocSensible[] }>("GET", "/api/sensibles");
  return Array.isArray(r.documents) ? r.documents : [];
}

async function lireTrousseau(): Promise<Trousseau | null> {
  const r = await api<{ trousseau?: Trousseau | null }>("GET", "/api/sensibles/trousseau");
  const t = r.trousseau;
  return t && typeof t.cle === "string" && Array.isArray(t.enveloppes) ? t : null;
}

async function ecrireTrousseau(t: Trousseau): Promise<void> {
  await api("PUT", "/api/sensibles/trousseau", { version: 1, cle: t.cle, enveloppes: t.enveloppes });
  trousseau = t;
  ecrireSels(linuxUser(), t.enveloppes);
}

/** Blob chiffré d'un document (step-up redemandé si le jeton a expiré). */
async function lireBlob(id: string): Promise<Octets> {
  const chemin = `/api/sensibles/${encodeURIComponent(id)}`;
  let r = await requeteRessayee("GET", chemin);
  if (r.status === 403) {
    const err = await lireErreur(r);
    if (!err.stepUp) throw err;
    oublierStepUp();
    await confirmerIdentite();
    r = await requeteRessayee("GET", chemin);
  }
  if (!r.ok) throw await lireErreur(r);
  return new Uint8Array(await r.arrayBuffer());
}

function erreurDouce(e: unknown): string {
  if (e instanceof ErreurApi && e.statut === 403 && !e.stepUp) return "Connexion de secours : tes documents sensibles restent fermés. Connecte-toi avec ton empreinte pour les ouvrir.";
  return messageErreur(e);
}

/** Ajoute à la clé retrouvée (brute, 32 octets) l'enveloppe de la passkey qui n'en avait pas, ou une clé d'appareil. */
async function proteger(brute: Octets, kid: string): Promise<Protection> {
  const u = linuxUser();
  if (prfEnAttente) {
    const { credentialId, sel, sortie } = prfEnAttente;
    prfEnAttente = null;
    try {
      const env = await envelopperPrf(await deriverKek(sortie, sel), brute, kid, credentialId, sel);
      const autres = trousseau && trousseau.cle === kid ? trousseau.enveloppes.filter((e) => e.credential_id !== credentialId) : [];
      await ecrireTrousseau({ version: 1, cle: kid, enveloppes: [...autres.slice(-9), env] });
      return "empreinte";
    } catch {
      /* repli : clé d'appareil */
    } finally {
      effacerOctets(sortie);
    }
  }
  const kek = await genererKekAppareil();
  const ok = await ecrireClesAppareil(u, { kek, enveloppe: await envelopperAppareil(kek, brute, kid) });
  return ok ? "appareil" : "phrase";
}

// --- déverrouillage -----------------------------------------------------------------------------------------------

/** Empreinte (step-up avec PRF), puis clé maîtresse : enveloppe de la passkey, sinon clé de l'appareil, sinon phrase. */
export async function deverrouiller(): Promise<void> {
  if (etat.phase === "ouvert" || etat.occupe) return;
  if (enSecours()) {
    definir({ phase: "secours" });
    return;
  }
  const u = linuxUser();
  definir({ phase: "ouverture", occupe: true, erreur: null });
  const selNeuf = aleatoire(32);
  const sels = lireSels(u);
  try {
    const r = await confirmerIdentitePrf((autorises) => {
      const parPasskey: Record<string, { first: Octets }> = {};
      for (const s of sels) if (autorises.includes(s.credential_id)) parPasskey[s.credential_id] = { first: depuisB64u(s.sel) };
      return { eval: { first: selNeuf }, ...(Object.keys(parPasskey).length ? { evalByCredential: parPasskey } : {}) };
    });
    const selDemande = sels.find((s) => s.credential_id === r.credentialId);
    const selUtilise = selDemande ? depuisB64u(selDemande.sel) : selNeuf;
    const [documents, t] = await Promise.all([lireListe(), lireTrousseau()]);
    trousseau = t;
    if (t) ecrireSels(u, t.enveloppes);
    const attendu = kidAttendu(documents);
    let brute: Octets | null = null;
    let kid: string | null = null;
    let protection: Protection = "empreinte";

    if (r.prf) {
      const env = t?.enveloppes.find((e) => e.credential_id === r.credentialId);
      if (env && t) {
        let sortie: Octets | null = r.prf;
        const selEnveloppe = depuisB64u(env.sel);
        if (!egaux(selEnveloppe, selUtilise)) {
          // Sel inconnu de cet appareil (première ouverture ici) : une seconde empreinte avec le bon sel.
          effacerOctets(sortie);
          const r2 = await confirmerIdentitePrf((autorises) => (autorises.includes(env.credential_id) ? { evalByCredential: { [env.credential_id]: { first: selEnveloppe } } } : null));
          sortie = r2.credentialId === env.credential_id ? r2.prf : (effacerOctets(r2.prf), null);
        }
        if (sortie) {
          try {
            brute = await ouvrirEnveloppePrf(await deriverKek(sortie, selEnveloppe), env, t.cle);
            kid = t.cle;
          } catch {
            /* enveloppe d'une autre clé : clé d'appareil ou phrase */
          } finally {
            effacerOctets(sortie);
          }
        }
      } else {
        prfEnAttente = { credentialId: r.credentialId, sel: selUtilise, sortie: r.prf };
      }
    }

    if (!brute) {
      const appareil = await lireClesAppareil(u);
      if (appareil && (!attendu || appareil.enveloppe.kid === attendu)) {
        try {
          brute = await ouvrirEnveloppeAppareil(appareil.kek, appareil.enveloppe);
          kid = appareil.enveloppe.kid;
          protection = "appareil";
        } catch {
          /* clé d'appareil d'une autre clé maîtresse */
        }
      }
      if (!brute && !t && !appareil && !attendu && documents.length === 0) {
        await preparerCreation(documents);
        return;
      }
    }

    if (brute && kid) {
      if (prfEnAttente) protection = await proteger(brute, kid);
      const nouvelle = await importerCle(brute);
      effacerOctets(brute);
      ouvrir(nouvelle, kid, protection, documents);
      return;
    }
    definir({ phase: "phrase", documents, occupe: false });
  } catch (e) {
    if (prfEnAttente) effacerOctets(prfEnAttente.sortie);
    prfEnAttente = null;
    const secours = e instanceof ErreurApi && e.statut === 403 && !e.stepUp;
    definir({ phase: secours ? "secours" : "verrouille", occupe: false, erreur: secours ? null : erreurDouce(e) });
  }
}

async function preparerCreation(documents: DocSensible[]): Promise<void> {
  const cmd = aleatoire(32);
  const kid = aleatoire(16);
  creation = { cmd, kid, prf: prfEnAttente };
  prfEnAttente = null;
  const phrase = await phraseDepuisCle(cmd);
  if (creation?.cmd !== cmd) return;
  surveiller(true); // la phrase affichée est une clé : même verrouillage automatique que les documents
  definir({ phase: "creation", phrase, documents, occupe: false, protection: creation.prf ? "empreinte" : "appareil" });
}

/** Phrase notée (dernier groupe retapé) : la clé est protégée (passkey ou appareil) puis ouverte. */
export async function confirmerCreation(): Promise<void> {
  const c = creation;
  if (!c || etat.occupe) return;
  definir({ occupe: true, erreur: null });
  const kid = versHex(c.kid);
  try {
    let protection: Protection = "appareil";
    if (c.prf) {
      const env = await envelopperPrf(await deriverKek(c.prf.sortie, c.prf.sel), c.cmd, kid, c.prf.credentialId, c.prf.sel);
      await ecrireTrousseau({ version: 1, cle: kid, enveloppes: [env] });
      protection = "empreinte";
    } else {
      const kek = await genererKekAppareil();
      if (!(await ecrireClesAppareil(linuxUser(), { kek, enveloppe: await envelopperAppareil(kek, c.cmd, kid) }))) protection = "phrase";
    }
    const nouvelle = await importerCle(c.cmd);
    effacerOctets(c.cmd, c.prf?.sortie);
    creation = null;
    ouvrir(nouvelle, kid, protection, etat.documents ?? []);
  } catch (e) {
    definir({ occupe: false, erreur: erreurDouce(e) });
  }
}

export function annulerCreation(): void {
  verrouiller("manuel");
}

/** Phrase de secours : somme de contrôle, puis essai sur un document du bon identifiant de clé avant d'accepter. */
export async function ouvrirAvecPhrase(saisie: string): Promise<void> {
  if (etat.occupe) return;
  definir({ occupe: true, erreur: null });
  let brute: Octets | null = null;
  try {
    brute = await cleDepuisPhrase(saisie);
    const documents = etat.documents ?? (await lireListe());
    const kid = kidAttendu(documents);
    if (!kid) throw new CleInvalide();
    const nouvelle = await importerCle(brute);
    const essai = documents.filter((d) => d.cle === kid).sort((a, b) => a.taille - b.taille)[0];
    if (essai) await dechiffrerDocument(nouvelle, await lireBlob(essai.id));
    const protection = await proteger(brute, kid);
    ouvrir(nouvelle, kid, protection, documents);
  } catch (e) {
    const message =
      e instanceof PhraseInvalide ? e.message : e instanceof CleInvalide || e instanceof DocumentInvalide ? "Cette phrase n’ouvre pas tes documents : vérifie-la." : erreurDouce(e);
    definir({ occupe: false, erreur: message });
  } finally {
    effacerOctets(brute);
  }
}

// --- documents ------------------------------------------------------------------------------------------------------

const TYPES_AFFICHABLES = /^(image\/(jpeg|png|webp|gif|heic|heif|avif)|application\/pdf)$/;

function typeDe(fichier: File): string {
  if (fichier.type) return fichier.type;
  const ext = fichier.name.split(".").pop()?.toLowerCase();
  return ext === "pdf" ? "application/pdf" : ext === "png" ? "image/png" : ext === "heic" ? "image/heic" : ext === "webp" ? "image/webp" : "image/jpeg";
}

/** Fichier choisi depuis le menu « + » ou l'écran : en attente du déverrouillage puis du libellé. */
export function preparerDepot(fichier: File | null): void {
  definir({ depot: fichier, erreur: null });
}

/** Chiffre le fichier dans le navigateur (enveloppe HSB1) puis l'envoie : le serveur ne reçoit que du chiffré. */
export async function deposer(fichier: File, nom: string, categorie: string): Promise<boolean> {
  if (!cle || !kidCourant) {
    definir({ erreur: "Déverrouille d’abord tes documents." });
    return false;
  }
  if (fichier.size > TAILLE_MAX) {
    definir({ erreur: "Document trop gros (20 Mo au plus)." });
    return false;
  }
  definir({ occupe: true, erreur: null });
  let contenu: Octets | null = null;
  try {
    contenu = new Uint8Array(await fichier.arrayBuffer());
    const type = typeDe(fichier);
    const blob = await chiffrerDocument(cle, depuisHex(kidCourant), { nom: fichier.name, type, taille: contenu.length, cree: new Date().toISOString().slice(0, 10) }, contenu);
    const fd = new FormData();
    fd.append("blob", new Blob([blob], { type: "application/octet-stream" }), "document.bin");
    fd.append("nom", nom.trim().slice(0, 80));
    fd.append("categorie", categorie);
    const r = await api<{ document: DocSensible }>("POST", "/api/sensibles", fd);
    definir({ documents: [r.document, ...(etat.documents ?? []).filter((d) => d.id !== r.document.id)], occupe: false, depot: null });
    toast("Document chiffré et rangé.");
    return true;
  } catch (e) {
    definir({ occupe: false, erreur: erreurDouce(e) });
    return false;
  } finally {
    effacerOctets(contenu);
  }
}

/** Déchiffre en mémoire et affiche depuis une URL `blob:` (révoquée à la fermeture et au verrouillage). */
export async function ouvrirDocument(doc: DocSensible): Promise<void> {
  if (!cle || !kidCourant) {
    documentAOuvrir = doc.id;
    await deverrouiller();
    return;
  }
  definir({ occupe: true, erreur: null });
  try {
    const blob = await lireBlob(doc.id);
    if (kidDuBlob(blob) !== kidCourant) throw new CleInvalide("Ce document a été protégé par une autre clé : ouvre-le avec la phrase de secours de l’époque.");
    const { meta, contenu } = await dechiffrerDocument(cle, blob);
    if (!cle) return; // verrouillé pendant le déchiffrement
    const type = TYPES_AFFICHABLES.test(meta.type) ? meta.type : "application/octet-stream";
    const url = URL.createObjectURL(new Blob([contenu], { type }));
    effacerOctets(contenu);
    fermerApercu();
    definir({ apercu: { id: doc.id, nom: doc.nom, type, url, nomOrigine: meta.nom }, occupe: false });
  } catch (e) {
    definir({ occupe: false, erreur: e instanceof CleInvalide ? e.message : erreurDouce(e) });
  }
}

export function fermerDocument(): void {
  fermerApercu();
  definir({ apercu: null });
}

export async function supprimer(doc: DocSensible): Promise<boolean> {
  definir({ occupe: true, erreur: null });
  try {
    await api("DELETE", `/api/sensibles/${encodeURIComponent(doc.id)}`);
    if (etat.apercu?.id === doc.id) fermerDocument();
    definir({ documents: (etat.documents ?? []).filter((d) => d.id !== doc.id), occupe: false });
    toast("Document supprimé.");
    return true;
  } catch (e) {
    definir({ occupe: false, erreur: erreurDouce(e) });
    return false;
  }
}

/** Carte `document_sensible` du fil (action `deverrouiller`, `args.id`) : même parcours, document ouvert ensuite. */
export async function demanderOuverture(id?: string): Promise<void> {
  const doc = id ? etat.documents?.find((d) => d.id === id) : null;
  if (doc && cle) return ouvrirDocument(doc);
  documentAOuvrir = id ?? null;
  await deverrouiller();
}

export function effacerErreur(): void {
  definir({ erreur: null });
}

/** Phase de départ selon la session (connexion de secours : documents fermés, message doux). */
export function initialiser(): void {
  if (etat.phase === "verrouille" && enSecours()) definir({ phase: "secours" });
  if (etat.phase === "secours" && !enSecours()) definir({ phase: "verrouille" });
}

if (typeof window !== "undefined") {
  quandDeconnecte(() => verrouiller("deconnexion"));
  // Dépôt ou suppression depuis un autre onglet ou appareil : liste relue si les documents sont ouverts.
  window.addEventListener(EVENEMENT_MAJ, (e) => {
    const s = (e as CustomEvent).detail?.sujet;
    if ((s === "sensibles" || s === "*") && etat.phase === "ouvert" && !etat.occupe) {
      void lireListe()
        .then((documents) => etat.phase === "ouvert" && definir({ documents }))
        .catch(() => {});
    }
  });
}

/** Pour les tests : état initial, secrets oubliés. */
export function reinitialiser(): void {
  verrouiller("deconnexion");
  trousseau = null;
  etat = ETAT_INITIAL;
}

export const etatSensibles = (): EtatSensibles => etat;

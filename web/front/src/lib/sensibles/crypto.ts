// Documents sensibles : chiffrement dans le navigateur (docs/documents-sensibles.md, CONTRACT §12).
// Fonctions pures autour de WebCrypto, sans état ni réseau : enveloppe HSB1 des documents, enveloppes de la clé
// maîtresse (passkey ou appareil), phrase de récupération. Implémentation de référence en Python :
// tests/test_web_sensibles.py::test_schema_de_reference_aller_retour (vecteurs communs : tests/fixtures/sensibles-vecteurs.json).
// Règle : aucune clé, sortie de passkey, phrase ni contenu n'est journalisé ici (ni console, ni erreur détaillée).

export type Octets = Uint8Array<ArrayBuffer>;

export const MAGIC = "HSB1";
export const VERSION = 1;
export const LONGUEUR_ENTETE = 4 + 1 + 16 + 12; // magic, version, kid, iv : données associées du chiffrement
export const LONGUEUR_MIN = LONGUEUR_ENTETE + 16; // + étiquette GCM
export const INFO_KEK = "homeos-sensibles-v1/kek";
export const PREFIXE_AAD = "homeos-sensibles-v1|";
export const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"; // base32 Crockford

const enc = new TextEncoder();
const dec = new TextDecoder();

/** Erreur de déchiffrement sans détail (mauvaise clé, blob altéré) : jamais la cause cryptographique à l'écran. */
export class CleInvalide extends Error {
  constructor(message = "Cette clé n’ouvre pas ce document.") {
    super(message);
    this.name = "CleInvalide";
  }
}

export class DocumentInvalide extends Error {
  constructor(message = "Document illisible.") {
    super(message);
    this.name = "DocumentInvalide";
  }
}

const subtil = (): SubtleCrypto => {
  const s = globalThis.crypto?.subtle;
  if (!s) throw new Error("Chiffrement indisponible dans ce navigateur.");
  return s;
};

export function aleatoire(n: number): Octets {
  const o = new Uint8Array(n);
  globalThis.crypto.getRandomValues(o);
  return o;
}

export function concat(...parts: Uint8Array[]): Octets {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const sortie = new Uint8Array(total);
  let i = 0;
  for (const p of parts) {
    sortie.set(p, i);
    i += p.length;
  }
  return sortie;
}

export const utf8 = (t: string): Octets => enc.encode(t) as Octets;

export function versHex(o: Uint8Array): string {
  return Array.from(o, (x) => x.toString(16).padStart(2, "0")).join("");
}

export function depuisHex(h: string): Octets {
  if (!/^(?:[0-9a-f]{2})*$/i.test(h)) throw new DocumentInvalide();
  const o = new Uint8Array(h.length / 2);
  for (let i = 0; i < o.length; i++) o[i] = parseInt(h.slice(2 * i, 2 * i + 2), 16);
  return o;
}

/** base64url sans remplissage (format du trousseau et des identifiants de passkey). */
export function versB64u(o: Uint8Array): string {
  let binaire = "";
  for (const x of o) binaire += String.fromCharCode(x);
  return btoa(binaire).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function depuisB64u(t: string): Octets {
  const normal = t.replace(/-/g, "+").replace(/_/g, "/");
  const binaire = atob(normal.padEnd(Math.ceil(normal.length / 4) * 4, "="));
  return Uint8Array.from(binaire, (c) => c.charCodeAt(0));
}

export function egaux(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a[i] ^ b[i];
  return d === 0;
}

// --- clés --------------------------------------------------------------------------------------------------------

/** Clé maîtresse des documents (32 octets bruts) → CryptoKey AES-GCM **non extractible**. */
export function importerCle(brute: Uint8Array): Promise<CryptoKey> {
  return subtil().importKey("raw", brute as Octets, { name: "AES-GCM" }, false, ["encrypt", "decrypt"]);
}

/** Clé d'enveloppe d'une passkey : HKDF-SHA-256(sortie PRF, sel, "homeos-sensibles-v1/kek") → AES-GCM 256. */
export async function deriverKek(prf: Uint8Array, sel: Uint8Array): Promise<CryptoKey> {
  const base = await subtil().importKey("raw", prf as Octets, "HKDF", false, ["deriveKey"]);
  return subtil().deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: sel as Octets, info: utf8(INFO_KEK) },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

/** Clé d'appareil (repli sans PRF) : AES-GCM 256 non extractible, rangée telle quelle dans IndexedDB. */
export function genererKekAppareil(): Promise<CryptoKey> {
  return subtil().generateKey({ name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]) as Promise<CryptoKey>;
}

export const aadEnveloppe = (kidHex: string, destinataire: string): Octets => utf8(`${PREFIXE_AAD}${kidHex}|${destinataire}`);

export interface EnveloppePrf {
  type: "prf";
  credential_id: string;
  sel: string;
  iv: string;
  cle_chiffree: string;
  cree?: string;
}

export interface EnveloppeAppareil {
  type: "appareil";
  kid: string;
  iv: string;
  cle_chiffree: string;
}

async function chiffrerCle(kek: CryptoKey, cmd: Uint8Array, aad: Octets, iv: Octets): Promise<Octets> {
  return new Uint8Array(await subtil().encrypt({ name: "AES-GCM", iv, additionalData: aad }, kek, cmd as Octets));
}

async function dechiffrerCle(kek: CryptoKey, chiffre: Octets, aad: Octets, iv: Octets): Promise<Octets> {
  try {
    const brute = new Uint8Array(await subtil().decrypt({ name: "AES-GCM", iv, additionalData: aad }, kek, chiffre));
    if (brute.length !== 32) throw new CleInvalide();
    return brute;
  } catch {
    throw new CleInvalide();
  }
}

export async function envelopperPrf(
  kek: CryptoKey,
  cmd: Uint8Array,
  kidHex: string,
  credentialId: string,
  sel: Uint8Array,
  iv: Octets = aleatoire(12),
): Promise<EnveloppePrf> {
  const chiffre = await chiffrerCle(kek, cmd, aadEnveloppe(kidHex, credentialId), iv);
  return { type: "prf", credential_id: credentialId, sel: versB64u(sel), iv: versB64u(iv), cle_chiffree: versB64u(chiffre), cree: new Date().toISOString().slice(0, 10) };
}

/** Clé maîtresse brute (32 octets) depuis une enveloppe PRF ; à importer aussitôt puis à effacer (`fill(0)`). */
export function ouvrirEnveloppePrf(kek: CryptoKey, e: EnveloppePrf, kidHex: string): Promise<Octets> {
  return dechiffrerCle(kek, depuisB64u(e.cle_chiffree), aadEnveloppe(kidHex, e.credential_id), depuisB64u(e.iv));
}

export async function envelopperAppareil(kek: CryptoKey, cmd: Uint8Array, kidHex: string, iv: Octets = aleatoire(12)): Promise<EnveloppeAppareil> {
  const chiffre = await chiffrerCle(kek, cmd, aadEnveloppe(kidHex, "appareil"), iv);
  return { type: "appareil", kid: kidHex, iv: versB64u(iv), cle_chiffree: versB64u(chiffre) };
}

export function ouvrirEnveloppeAppareil(kek: CryptoKey, e: EnveloppeAppareil): Promise<Octets> {
  return dechiffrerCle(kek, depuisB64u(e.cle_chiffree), aadEnveloppe(e.kid, "appareil"), depuisB64u(e.iv));
}

// --- documents (enveloppe HSB1) ------------------------------------------------------------------------------------

export interface MetaDocument {
  nom: string;
  type: string;
  taille: number;
  cree?: string;
}

/** Enveloppe HSB1 : "HSB1" | 0x01 | kid (16) | iv (12) | AES-256-GCM(CMD, iv, aad = 33 premiers octets). */
export async function chiffrerDocument(cle: CryptoKey, kid: Uint8Array, meta: MetaDocument, contenu: Uint8Array, iv: Octets = aleatoire(12)): Promise<Octets> {
  if (kid.length !== 16 || iv.length !== 12) throw new DocumentInvalide();
  const entete = concat(utf8(MAGIC), new Uint8Array([VERSION]), kid, iv);
  const json = utf8(JSON.stringify(meta));
  const longueur = new Uint8Array(4);
  new DataView(longueur.buffer).setUint32(0, json.length, false);
  const clair = concat(longueur, json, contenu);
  const chiffre = new Uint8Array(await subtil().encrypt({ name: "AES-GCM", iv, additionalData: entete }, cle, clair));
  clair.fill(0);
  return concat(entete, chiffre);
}

/** Identifiant de clé (hex) lu dans l'en-tête, ou erreur si ce n'est pas une enveloppe HSB1 version 1. */
export function kidDuBlob(blob: Uint8Array): string {
  if (blob.length < LONGUEUR_MIN || dec.decode(blob.subarray(0, 4)) !== MAGIC || blob[4] !== VERSION) throw new DocumentInvalide();
  return versHex(blob.subarray(5, 21));
}

export async function dechiffrerDocument(cle: CryptoKey, blob: Uint8Array): Promise<{ meta: MetaDocument; contenu: Octets; kid: string }> {
  const kid = kidDuBlob(blob);
  const entete = blob.slice(0, LONGUEUR_ENTETE);
  let clair: Octets;
  try {
    clair = new Uint8Array(await subtil().decrypt({ name: "AES-GCM", iv: blob.slice(21, 33), additionalData: entete }, cle, blob.slice(LONGUEUR_ENTETE)));
  } catch {
    throw new CleInvalide();
  }
  if (clair.length < 4) throw new DocumentInvalide();
  const longueur = new DataView(clair.buffer, clair.byteOffset, 4).getUint32(0, false);
  if (4 + longueur > clair.length) throw new DocumentInvalide();
  let meta: MetaDocument;
  try {
    const brut = JSON.parse(dec.decode(clair.subarray(4, 4 + longueur)));
    meta = { nom: String(brut.nom ?? ""), type: String(brut.type ?? ""), taille: Number(brut.taille) || 0, ...(brut.cree ? { cree: String(brut.cree) } : {}) };
  } catch {
    throw new DocumentInvalide();
  }
  return { meta, contenu: clair.slice(4 + longueur), kid };
}

// --- phrase de récupération --------------------------------------------------------------------------------------

/** CMD (32 octets) || SHA-256(CMD)[0..2] en base32 Crockford : 55 caractères, 11 groupes de 5 séparés par des tirets. */
export async function phraseDepuisCle(cmd: Uint8Array): Promise<string> {
  if (cmd.length !== 32) throw new CleInvalide();
  const somme = new Uint8Array(await subtil().digest("SHA-256", cmd as Octets)).subarray(0, 2);
  const brut = concat(cmd, somme);
  let bits = "";
  for (const o of brut) bits += o.toString(2).padStart(8, "0");
  bits += "0".repeat((5 - (bits.length % 5)) % 5);
  let texte = "";
  for (let i = 0; i < bits.length; i += 5) texte += ALPHABET[parseInt(bits.slice(i, i + 5), 2)];
  brut.fill(0);
  return groupes(texte).join("-");
}

export const groupes = (texte: string): string[] => texte.match(/.{1,5}/g) ?? [];

/** Saisie normalisée : majuscules, O→0, I/L→1, tirets et espaces ignorés. */
export function normaliserPhrase(saisie: string): string {
  return saisie
    .toUpperCase()
    .replace(/[\s\-_.]/g, "")
    .replace(/O/g, "0")
    .replace(/[IL]/g, "1");
}

export class PhraseInvalide extends Error {
  constructor(message = "Phrase incomplète ou mal recopiée : vérifie chaque groupe.") {
    super(message);
    this.name = "PhraseInvalide";
  }
}

/** Clé maîtresse brute depuis la phrase (somme de contrôle vérifiée) ; à importer puis effacer. */
export async function cleDepuisPhrase(saisie: string): Promise<Octets> {
  const texte = normaliserPhrase(saisie);
  if (texte.length !== 55) throw new PhraseInvalide();
  let bits = "";
  for (const c of texte) {
    const v = ALPHABET.indexOf(c);
    if (v < 0) throw new PhraseInvalide();
    bits += v.toString(2).padStart(5, "0");
  }
  if (/1/.test(bits.slice(272))) throw new PhraseInvalide(); // bits de remplissage non nuls
  const brut = new Uint8Array(34);
  for (let i = 0; i < 34; i++) brut[i] = parseInt(bits.slice(8 * i, 8 * i + 8), 2);
  const cmd = brut.slice(0, 32);
  const somme = new Uint8Array(await subtil().digest("SHA-256", cmd)).subarray(0, 2);
  const ok = egaux(somme, brut.subarray(32));
  brut.fill(0);
  if (!ok) {
    cmd.fill(0);
    throw new PhraseInvalide();
  }
  return cmd;
}

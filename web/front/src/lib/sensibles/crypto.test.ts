// @vitest-environment node
// Interopérabilité avec l'implémentation de référence en Python (tests/sensibles_reference.py) : mêmes vecteurs,
// mêmes octets, dans les deux sens ; phrase de récupération ; refus d'un blob altéré ou d'une mauvaise clé.
import { describe, expect, it } from "vitest";
import vecteurs from "../../../../../tests/fixtures/sensibles-vecteurs.json";
import {
  CleInvalide,
  DocumentInvalide,
  PhraseInvalide,
  chiffrerDocument,
  cleDepuisPhrase,
  dechiffrerDocument,
  depuisHex,
  deriverKek,
  envelopperAppareil,
  envelopperPrf,
  importerCle,
  kidDuBlob,
  normaliserPhrase,
  ouvrirEnveloppeAppareil,
  ouvrirEnveloppePrf,
  phraseDepuisCle,
  versHex,
  aleatoire,
} from "./crypto";

const V = vecteurs;
const h = depuisHex;

async function kekBrute(brute: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", brute as Uint8Array<ArrayBuffer>, "AES-GCM", false, ["encrypt", "decrypt"]);
}

describe("documents sensibles : interopérabilité avec la référence Python", () => {
  it("chiffre un document exactement comme la référence (même clé, même IV)", async () => {
    const cle = await importerCle(h(V.cmd));
    const blob = await chiffrerDocument(cle, h(V.kid), V.meta, h(V.contenu), h(V.iv_document));
    expect(versHex(blob)).toBe(V.document);
    expect(new TextDecoder().decode(blob.subarray(0, 4))).toBe("HSB1");
    expect(blob[4]).toBe(1);
    expect(kidDuBlob(blob)).toBe(V.kid);
  });

  it("déchiffre un document chiffré par la référence", async () => {
    const cle = await importerCle(h(V.cmd));
    const { meta, contenu, kid } = await dechiffrerDocument(cle, h(V.document));
    expect(meta).toEqual(V.meta);
    expect(versHex(contenu)).toBe(V.contenu);
    expect(kid).toBe(V.kid);
  });

  it("enveloppe PRF : HKDF + AES-GCM identiques, ouverture d'une enveloppe de la référence", async () => {
    const kek = await deriverKek(h(V.prf), h(V.sel));
    const env = await envelopperPrf(kek, h(V.cmd), V.kid, V.credential_id, h(V.sel), h(V.iv_enveloppe));
    const { cree, ...sansDate } = env;
    expect(cree).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(sansDate).toEqual(V.enveloppe_prf);
    const cmd = await ouvrirEnveloppePrf(kek, V.enveloppe_prf as never, V.kid);
    expect(versHex(cmd)).toBe(V.cmd);
    // Une autre sortie de passkey (ou un autre sel) n'ouvre rien.
    await expect(ouvrirEnveloppePrf(await deriverKek(aleatoire(32), h(V.sel)), V.enveloppe_prf as never, V.kid)).rejects.toBeInstanceOf(CleInvalide);
    // L'enveloppe est liée à la passkey et au kid (données associées).
    await expect(ouvrirEnveloppePrf(kek, { ...(V.enveloppe_prf as never as object), credential_id: "autre" } as never, V.kid)).rejects.toBeInstanceOf(CleInvalide);
  });

  it("enveloppe d'appareil identique à la référence", async () => {
    const kek = await kekBrute(h(V.kek_appareil));
    expect(await envelopperAppareil(kek, h(V.cmd), V.kid, h(V.iv_appareil))).toEqual(V.enveloppe_appareil);
    expect(versHex(await ouvrirEnveloppeAppareil(kek, V.enveloppe_appareil as never))).toBe(V.cmd);
  });

  it("phrase de récupération : 11 groupes de 5, identique à la référence, relue avec tolérance", async () => {
    const phrase = await phraseDepuisCle(h(V.cmd));
    expect(phrase).toBe(V.phrase);
    expect(phrase.split("-")).toHaveLength(11);
    expect(phrase.replace(/-/g, "")).toHaveLength(55);
    expect(versHex(await cleDepuisPhrase(phrase))).toBe(V.cmd);
    // Minuscules, espaces, O pour 0, l pour 1 : acceptés.
    const tapee = phrase.toLowerCase().replace(/-/g, " ").replace(/0/g, "o").replace(/1/g, "l");
    expect(versHex(await cleDepuisPhrase(tapee))).toBe(V.cmd);
    expect(normaliserPhrase("ab-cd o i l")).toBe("ABCD011");
  });

  it("phrase mal recopiée : somme de contrôle refusée", async () => {
    const lettres = V.phrase.replace(/-/g, "").split("");
    lettres[10] = lettres[10] === "A" ? "B" : "A";
    await expect(cleDepuisPhrase(lettres.join(""))).rejects.toBeInstanceOf(PhraseInvalide);
    await expect(cleDepuisPhrase(V.phrase.slice(0, -1))).rejects.toBeInstanceOf(PhraseInvalide);
    await expect(cleDepuisPhrase(V.phrase.replace(/.$/, "U"))).rejects.toBeInstanceOf(PhraseInvalide);
  });

  it("aller-retour avec une clé neuve ; blob altéré ou mauvaise clé : refus sans détail", async () => {
    const cle = await importerCle(aleatoire(32));
    const kid = aleatoire(16);
    const contenu = Uint8Array.from({ length: 70_000 }, (_, i) => (i * 31) % 256);
    const blob = await chiffrerDocument(cle, kid, { nom: "photo.jpg", type: "image/jpeg", taille: contenu.length }, contenu);
    expect(blob.length).toBe(33 + 4 + JSON.stringify({ nom: "photo.jpg", type: "image/jpeg", taille: contenu.length }).length + contenu.length + 16);
    const lu = await dechiffrerDocument(cle, blob);
    expect(lu.meta.nom).toBe("photo.jpg");
    expect(versHex(lu.contenu)).toBe(versHex(contenu));
    const altere = blob.slice();
    altere[40] ^= 1;
    await expect(dechiffrerDocument(cle, altere)).rejects.toBeInstanceOf(CleInvalide);
    const entete = blob.slice();
    entete[10] ^= 1; // kid modifié : l'en-tête est authentifié
    await expect(dechiffrerDocument(cle, entete)).rejects.toBeInstanceOf(CleInvalide);
    await expect(dechiffrerDocument(await importerCle(aleatoire(32)), blob)).rejects.toBeInstanceOf(CleInvalide);
    expect(() => kidDuBlob(new TextEncoder().encode("%PDF-1.4 en clair, pas une enveloppe chiffrée HSB1"))).toThrow(DocumentInvalide);
  });

  it("la clé maîtresse importée n'est pas extractible", async () => {
    const cle = await importerCle(aleatoire(32));
    expect(cle.extractable).toBe(false);
    await expect(crypto.subtle.exportKey("raw", cle)).rejects.toBeTruthy();
    expect((await deriverKek(aleatoire(32), aleatoire(32))).extractable).toBe(false);
  });
});

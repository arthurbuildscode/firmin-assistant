// Clé d'appareil des documents sensibles (repli sans passkey PRF) : IndexedDB, base `homeos-sensibles`, magasin `cles`.
//   kek-appareil:<linux_user>        CryptoKey AES-GCM non extractible (le navigateur la garde, illisible en clair)
//   enveloppe-appareil:<linux_user>  {type: "appareil", kid, iv, cle_chiffree} : clé maîtresse chiffrée par cette clé
// Ni l'une ni l'autre ne quitte l'appareil (le serveur refuse l'enveloppe d'appareil). La clé maîtresse n'est jamais
// écrite ici en clair. Tout accès est entouré : IndexedDB peut être absent (navigation privée, réglages).
import type { EnveloppeAppareil } from "./crypto";

const BASE = "homeos-sensibles";
const MAGASIN = "cles";

function ouvrir(): Promise<IDBDatabase> {
  return new Promise((ok, ko) => {
    if (typeof indexedDB === "undefined") return ko(new Error("IndexedDB indisponible"));
    const r = indexedDB.open(BASE, 1);
    r.onupgradeneeded = () => {
      if (!r.result.objectStoreNames.contains(MAGASIN)) r.result.createObjectStore(MAGASIN);
    };
    r.onsuccess = () => ok(r.result);
    r.onerror = () => ko(r.error ?? new Error("IndexedDB"));
  });
}

async function operation<T>(mode: IDBTransactionMode, fn: (m: IDBObjectStore) => IDBRequest | void): Promise<T | undefined> {
  const db = await ouvrir();
  try {
    return await new Promise<T | undefined>((ok, ko) => {
      const t = db.transaction(MAGASIN, mode);
      const req = fn(t.objectStore(MAGASIN));
      t.oncomplete = () => ok(req ? (req.result as T) : undefined);
      t.onerror = () => ko(t.error ?? new Error("IndexedDB"));
      t.onabort = () => ko(t.error ?? new Error("IndexedDB"));
    });
  } finally {
    db.close();
  }
}

const cleKek = (u: string) => `kek-appareil:${u}`;
const cleEnveloppe = (u: string) => `enveloppe-appareil:${u}`;

export interface ClesAppareil {
  kek: CryptoKey;
  enveloppe: EnveloppeAppareil;
}

/** Clé et enveloppe de cet appareil pour la personne, ou null (absentes, ou IndexedDB indisponible). */
export async function lireClesAppareil(linuxUser: string): Promise<ClesAppareil | null> {
  try {
    const kek = await operation<CryptoKey>("readonly", (m) => m.get(cleKek(linuxUser)));
    const enveloppe = await operation<EnveloppeAppareil>("readonly", (m) => m.get(cleEnveloppe(linuxUser)));
    if (!kek || !enveloppe || enveloppe.type !== "appareil") return null;
    return { kek, enveloppe };
  } catch {
    return null;
  }
}

/** Range la clé d'appareil (CryptoKey non extractible) et l'enveloppe ; false si IndexedDB est indisponible. */
export async function ecrireClesAppareil(linuxUser: string, cles: ClesAppareil): Promise<boolean> {
  try {
    await operation("readwrite", (m) => {
      m.put(cles.kek, cleKek(linuxUser));
      m.put(cles.enveloppe, cleEnveloppe(linuxUser));
    });
    return true;
  } catch {
    return false;
  }
}

export async function effacerClesAppareil(linuxUser: string): Promise<void> {
  try {
    await operation("readwrite", (m) => {
      m.delete(cleKek(linuxUser));
      m.delete(cleEnveloppe(linuxUser));
    });
  } catch {
    /* rien à effacer */
  }
}

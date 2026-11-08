// Documents sensibles : parcours du front contre un faux serveur en mémoire (mêmes règles que web/sensibles.py :
// blobs HSB1 seulement, enveloppes PRF seulement dans le trousseau). Passkey simulée : sortie PRF déterministe
// par passkey et par sel (comme un authentificateur réel).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { definirSession } from "@/lib/session";
import { aleatoire, cleDepuisPhrase, dechiffrerDocument, depuisB64u, phraseDepuisCle, deriverKek, importerCle, ouvrirEnveloppePrf, utf8, versHex, type EnveloppePrf } from "@/lib/sensibles/crypto";

const serveur = vi.hoisted(() => ({
  documents: [] as { id: string; nom: string; categorie: string; date: string; taille: number; cle: string; blob: Uint8Array }[],
  trousseau: null as null | { version: 1; cle: string; enveloppes: unknown[] },
  appels: [] as string[],
  passkey: { id: "cGFzc2tleS1h", prf: true },
}));

async function lireBlob(b: Blob): Promise<Uint8Array> {
  return new Promise((ok) => {
    const r = new FileReader();
    r.onload = () => ok(new Uint8Array(r.result as ArrayBuffer));
    r.readAsArrayBuffer(b);
  });
}

/** Sortie PRF simulée : SHA-256(secret de la passkey || sel). */
async function prfSimule(credentialId: string, sel: Uint8Array): Promise<Uint8Array<ArrayBuffer>> {
  const entree = new Uint8Array([...utf8(`secret:${credentialId}`), ...sel]);
  return new Uint8Array(await crypto.subtle.digest("SHA-256", entree));
}

vi.mock("@/lib/api", async (original) => {
  const vrai = await original<typeof import("@/lib/api")>();
  const meta = (d: (typeof serveur.documents)[number]) => ({ id: d.id, nom: d.nom, categorie: d.categorie, date: d.date, taille: d.taille, cle: d.cle });
  return {
    ...vrai,
    confirmerIdentite: vi.fn(async () => {}),
    confirmerIdentitePrf: vi.fn(async (entree: (autorises: string[]) => Record<string, unknown> | null) => {
      serveur.appels.push("empreinte");
      const id = serveur.passkey.id;
      const prf = entree([id]) as { eval?: { first: Uint8Array }; evalByCredential?: Record<string, { first: Uint8Array }> } | null;
      const sel = prf?.evalByCredential?.[id]?.first ?? prf?.eval?.first;
      return { credentialId: id, prf: serveur.passkey.prf && sel ? await prfSimule(id, sel) : null };
    }),
    requeteRessayee: vi.fn(async (_m: string, chemin: string) => {
      const d = serveur.documents.find((x) => chemin.endsWith(x.id));
      return d ? new Response(d.blob.slice(), { status: 200, headers: { "X-Homeos-Cle": d.cle } }) : new Response("{}", { status: 404 });
    }),
    api: vi.fn(async (methode: string, chemin: string, corps?: unknown) => {
      serveur.appels.push(`${methode} ${chemin}`);
      if (methode === "GET" && chemin === "/api/sensibles") return { documents: serveur.documents.map(meta), trousseau: !!serveur.trousseau };
      if (methode === "GET" && chemin === "/api/sensibles/trousseau") return { trousseau: serveur.trousseau };
      if (methode === "PUT" && chemin === "/api/sensibles/trousseau") {
        const t = corps as { version: 1; cle: string; enveloppes: { type: string }[] };
        if (t.enveloppes.some((e) => e.type !== "prf" || "prf" in e)) throw new vrai.ErreurApi("enveloppe refusée", 400);
        serveur.trousseau = t;
        return { ok: true };
      }
      if (methode === "POST" && chemin === "/api/sensibles") {
        const fd = corps as FormData;
        const blob = await lireBlob(fd.get("blob") as Blob);
        if (new TextDecoder().decode(blob.slice(0, 4)) !== "HSB1" || blob[4] !== 1) throw new vrai.ErreurApi("Document chiffré attendu", 415);
        const d = { id: `s-${String(serveur.documents.length + 1).padStart(24, "0")}`, nom: String(fd.get("nom")), categorie: String(fd.get("categorie")), date: "2026-11-08T10:00:00+01:00", taille: blob.length, cle: versHex(blob.slice(5, 21)), blob };
        serveur.documents.unshift(d);
        return { document: meta(d) };
      }
      if (methode === "DELETE") {
        serveur.documents = serveur.documents.filter((d) => !chemin.endsWith(d.id));
        return { ok: true };
      }
      throw new vrai.ErreurApi("introuvable", 404);
    }),
  };
});

import * as sensibles from "./sensibles";
import { SectionSensibles } from "@/ecrans/Sensibles";

const fichier = (nom: string, type: string, contenu: Uint8Array) =>
  Object.assign(new Blob([contenu as Uint8Array<ArrayBuffer>], { type }), { name: nom, lastModified: 0, arrayBuffer: async () => contenu.slice().buffer }) as unknown as File;

beforeEach(() => {
  serveur.documents = [];
  serveur.trousseau = null;
  serveur.appels = [];
  serveur.passkey = { id: "cGFzc2tleS1h", prf: true };
  localStorage.clear();
  definirSession({ connecte: true, personne: { prenom: "Alice", linux_user: "alice", role: "proprietaire" }, assistant: "Firmin", niveau: "passkey" });
  sensibles.reinitialiser();
  URL.createObjectURL = vi.fn(() => "blob:http://localhost/apercu");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  vi.useRealTimers();
});

async function premiereFois(): Promise<string> {
  await sensibles.deverrouiller();
  const e = sensibles.etatSensibles();
  expect(e.phase).toBe("creation");
  expect(e.phrase).toMatch(/^([0-9A-HJKMNP-TV-Z]{5}-){10}[0-9A-HJKMNP-TV-Z]{5}$/);
  const phrase = e.phrase!;
  await sensibles.confirmerCreation();
  expect(sensibles.etatSensibles().phase).toBe("ouvert");
  expect(sensibles.etatSensibles().phrase).toBeNull();
  return phrase;
}

describe("documents sensibles : parcours", () => {
  it("premier usage avec empreinte : phrase une fois, trousseau PRF, dépôt chiffré lisible avec la phrase", async () => {
    const phrase = await premiereFois();
    expect(sensibles.etatSensibles().protection).toBe("empreinte");
    // Trousseau : une enveloppe PRF qui s'ouvre avec la sortie de la passkey et son sel ; jamais la clé ni la sortie.
    const t = serveur.trousseau!;
    expect(t.enveloppes).toHaveLength(1);
    const env = t.enveloppes[0] as EnveloppePrf;
    expect(Object.keys(env).sort()).toEqual(["cle_chiffree", "credential_id", "cree", "iv", "sel", "type"]);
    const prf = await prfSimule(env.credential_id, depuisB64u(env.sel));
    const cmd = await ouvrirEnveloppePrf(await deriverKek(prf, depuisB64u(env.sel)), env, t.cle);
    expect(versHex(cmd)).toBe(versHex(await cleDepuisPhrase(phrase)));

    const contenu = utf8("\x89PNG photo du passeport");
    sensibles.preparerDepot(fichier("passeport.png", "image/png", contenu));
    expect(await sensibles.deposer(sensibles.etatSensibles().depot!, "Passeport", "identite")).toBe(true);
    const stocke = serveur.documents[0];
    expect(stocke.nom).toBe("Passeport");
    expect(stocke.cle).toBe(t.cle);
    expect(new TextDecoder().decode(stocke.blob)).not.toContain("passeport");
    const lu = await dechiffrerDocument(await importerCle(await cleDepuisPhrase(phrase)), stocke.blob);
    expect(lu.meta).toMatchObject({ nom: "passeport.png", type: "image/png", taille: contenu.length });
    expect(versHex(lu.contenu)).toBe(versHex(contenu));
    expect(sensibles.etatSensibles().depot).toBeNull();
  });

  it("réouverture : la même passkey ouvre la clé sans phrase ; ouverture en mémoire, nettoyée au verrouillage", async () => {
    await premiereFois();
    await sensibles.deposer(fichier("carnet.jpg", "image/jpeg", utf8("jpeg")), "Carnet de santé", "sante");
    sensibles.verrouiller("manuel");
    expect(sensibles.etatSensibles().phase).toBe("verrouille");
    expect(sensibles.etatSensibles().documents).toHaveLength(1); // libellés gardés, verrouillés

    serveur.appels = [];
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
    expect(serveur.appels.filter((a) => a === "empreinte")).toHaveLength(1); // sel connu : une seule empreinte
    await sensibles.ouvrirDocument(sensibles.etatSensibles().documents![0]);
    expect(sensibles.etatSensibles().apercu).toMatchObject({ nom: "Carnet de santé", type: "image/jpeg", nomOrigine: "carnet.jpg" });
    sensibles.verrouiller("arriere-plan");
    expect(sensibles.etatSensibles().apercu).toBeNull();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:http://localhost/apercu");
  });

  it("autre appareil sans le sel : seconde empreinte avec le bon sel", async () => {
    await premiereFois();
    sensibles.verrouiller("manuel");
    localStorage.clear();
    serveur.appels = [];
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
    expect(serveur.appels.filter((a) => a === "empreinte")).toHaveLength(2);
  });

  it("appareil sans la clé : phrase de secours (refus d'une phrase fausse), puis passkey ajoutée au trousseau", async () => {
    serveur.passkey = { id: "cGFzc2tleS1h", prf: false };
    const phrase = await premiereFois(); // sans PRF ni IndexedDB : seule la phrase protège
    expect(sensibles.etatSensibles().protection).toBe("phrase");
    expect(serveur.trousseau).toBeNull(); // jamais d'enveloppe d'appareil sur le serveur
    await sensibles.deposer(fichier("ordonnance.pdf", "application/pdf", utf8("%PDF-1.7")), "Ordonnance", "sante");
    sensibles.verrouiller("manuel");

    serveur.passkey = { id: "cGFzc2tleS1i", prf: true }; // nouvel appareil, passkey qui sait faire
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("phrase");
    await sensibles.ouvrirAvecPhrase("ABCDE");
    expect(sensibles.etatSensibles().erreur).toMatch(/mal recopiée/);
    expect(sensibles.etatSensibles().phase).toBe("phrase");
    await sensibles.ouvrirAvecPhrase(phrase.toLowerCase().replace(/-/g, " "));
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
    expect(serveur.trousseau!.enveloppes).toHaveLength(1);
    expect((serveur.trousseau!.enveloppes[0] as EnveloppePrf).credential_id).toBe("cGFzc2tleS1i");
    sensibles.verrouiller("manuel");
    await sensibles.deverrouiller(); // désormais l'empreinte suffit
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
  });

  it("une phrase valide mais d'une autre clé n'ouvre rien", async () => {
    serveur.passkey = { id: "cGFzc2tleS1h", prf: false };
    await premiereFois();
    await sensibles.deposer(fichier("a.png", "image/png", utf8("png")), "Passeport", "identite");
    sensibles.verrouiller("manuel");
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("phrase");
    await sensibles.ouvrirAvecPhrase(await phraseDepuisCle(aleatoire(32))); // somme de contrôle juste, autre clé
    expect(sensibles.etatSensibles().phase).toBe("phrase");
    expect(sensibles.etatSensibles().erreur).toMatch(/^Cette phrase n’ouvre pas tes documents\s:\svérifie-la\.$/);
  });

  it("verrouillage automatique après 5 minutes sans activité et au passage en arrière-plan", async () => {
    await premiereFois();
    vi.useFakeTimers();
    sensibles.verrouiller("manuel");
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
    vi.advanceTimersByTime(4 * 60_000);
    window.dispatchEvent(new Event("pointerdown")); // activité : délai relancé
    vi.advanceTimersByTime(4 * 60_000);
    expect(sensibles.etatSensibles().phase).toBe("ouvert");
    vi.advanceTimersByTime(60_001);
    expect(sensibles.etatSensibles().phase).toBe("verrouille");
    vi.useRealTimers();
    await sensibles.deverrouiller();
    Object.defineProperty(document, "visibilityState", { value: "hidden", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(sensibles.etatSensibles().phase).toBe("verrouille");
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  });

  it("suppression : retirée du serveur et de la liste", async () => {
    await premiereFois();
    await sensibles.deposer(fichier("a.png", "image/png", utf8("png")), "Passeport", "identite");
    expect(await sensibles.supprimer(sensibles.etatSensibles().documents![0])).toBe(true);
    expect(serveur.documents).toHaveLength(0);
    expect(sensibles.etatSensibles().documents).toHaveLength(0);
  });

  it("connexion de secours : aucune empreinte demandée, message doux", async () => {
    definirSession({ connecte: true, personne: { prenom: "Alice", linux_user: "alice", role: "proprietaire" }, assistant: "Firmin", niveau: "secours" });
    sensibles.reinitialiser();
    await sensibles.deverrouiller();
    expect(sensibles.etatSensibles().phase).toBe("secours");
    expect(serveur.appels).toHaveLength(0);
    render(<SectionSensibles />);
    expect(screen.getByText(/Connexion de secours : tes documents sensibles restent fermés/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Déverrouiller/ })).toBeNull();
  });
});

describe("documents sensibles : écran", () => {
  const MOTS_TECHNIQUES = /PRF|AES|HKDF|GCM|WebAuthn|IndexedDB|base32|HSB1|kid\b|chiffrement symétrique/i;

  it("verrouillé → première fois (phrase, confirmation du dernier groupe) → vide → dépôt → vignette", async () => {
    render(<SectionSensibles />);
    expect(document.body.textContent).toMatch(/Chiffrés, visibles par toi seul/);
    fireEvent.click(screen.getByRole("button", { name: /Déverrouiller/ }));
    const liste = await screen.findByLabelText(/Phrase de secours, 11 groupes/);
    expect(document.body.textContent).toMatch(/Note cette phrase sur papier\. Sans elle et sans ta passkey, tes documents sont perdus\./);
    expect(document.body.textContent).not.toMatch(MOTS_TECHNIQUES);
    const groupesAffiches = Array.from(liste.querySelectorAll("li")).map((li) => li.textContent);
    expect(groupesAffiches).toHaveLength(11);
    const bouton = screen.getByRole("button", { name: "Je l’ai notée" }) as HTMLButtonElement;
    expect(bouton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/retape le dernier groupe/), { target: { value: "zzzzz" } });
    expect(bouton.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/retape le dernier groupe/), { target: { value: groupesAffiches[10]!.toLowerCase() } });
    expect(bouton.disabled).toBe(false);
    fireEvent.click(bouton);
    await screen.findByText(/Aucun document pour l’instant/);
    expect(screen.getByText(/Protégé par ton empreinte · se verrouille après 5 minutes sans activité/)).toBeTruthy();
    expect(screen.queryByLabelText(/Phrase de secours, 11 groupes/)).toBeNull(); // affichée une seule fois

    await act(async () => sensibles.preparerDepot(fichier("IMG_2041.jpg", "image/jpeg", utf8("jpeg"))));
    await screen.findByText("Ajouter un document sensible");
    expect(document.body.textContent).not.toContain("IMG_2041");
    fireEvent.click(screen.getByRole("radio", { name: /Carnet de l’animal/ }));
    expect((screen.getByLabelText("Nom") as HTMLInputElement).value).toBe("Carnet de l’animal");
    fireEvent.click(screen.getByRole("button", { name: /Chiffrer et ranger/ }));
    await waitFor(() => expect(document.querySelectorAll("[data-document]")).toHaveLength(1));
    expect(serveur.documents[0].categorie).toBe("animal");
    expect(screen.getByRole("heading", { name: "Carnet de l’animal" })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(MOTS_TECHNIQUES);

    fireEvent.click(screen.getByRole("button", { name: "Verrouiller" }));
    expect(screen.getByRole("button", { name: /Déverrouiller Carnet de l’animal/ })).toBeTruthy();
    expect(document.body.textContent).toMatch(/Verrouillé · titulaire seul/);
  });

  it("clé absente de l'appareil : « utilise ta phrase de secours »", async () => {
    serveur.passkey = { id: "cGFzc2tleS1h", prf: false };
    await premiereFois();
    await sensibles.deposer(fichier("a.png", "image/png", utf8("png")), "Passeport", "identite");
    sensibles.verrouiller("manuel");
    render(<SectionSensibles />);
    fireEvent.click(screen.getByRole("button", { name: /^Déverrouiller$/ }));
    await screen.findByText("Cet appareil n’a pas la clé : utilise ta phrase de secours.");
    expect(screen.getByLabelText("Phrase de secours")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(MOTS_TECHNIQUES);
  });
});

// Données des écrans dédiés : points d'accès directs du backend (GET /api/courses, /api/echeances, /api/journal,
// /api/fichiers, /api/memoire), sans le modèle, relus à l'événement `maj` du flux. Tant qu'un point d'accès n'existe
// pas (404/405/501, ou réponse d'une autre forme), l'écran se replie sur la dernière carte du bon type reçue dans le
// fil : jamais d'erreur affichée pour un point d'accès absent.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ErreurApi } from "./api";

export type Sujet = "courses" | "echeances" | "journal" | "fichiers" | "memoire" | "sensibles";
export type StatutDonnees = "chargement" | "ok" | "absent";

const EVENEMENT = "homeos:maj";
const STATUTS_ABSENT = new Set([404, 405, 501]);

/** Signale que les données d'un sujet ont changé (événement `maj` du flux, fin d'un échange qui a touché le sujet). */
export function signalerMaj(sujet: Sujet | "*"): void {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(EVENEMENT, { detail: { sujet } }));
}

/** Sujet touché par une carte (pour relire l'écran concerné à la fin d'un échange). */
export function sujetDeCarte(type: string): Sujet | null {
  if (type === "courses") return "courses";
  if (type === "a_noter" || type === "echeance") return "echeances";
  if (type === "journal") return "journal";
  if (type === "fichier_classe" || type === "fichier_recu" || type === "document_sensible") return "fichiers";
  return null;
}

/**
 * Lit `/api/<sujet>` ; `valider` rend la donnée normalisée, ou null si la réponse n'a pas la forme attendue
 * (statut « absent » : repli sur les cartes du fil).
 */
export function useDonnees<T>(sujet: Sujet, valider: (brut: unknown) => T | null, chemin: string = `/api/${sujet}`) {
  const [etat, setEtat] = useState<{ statut: StatutDonnees; donnees: T | null }>({ statut: "chargement", donnees: null });
  const validerRef = useRef(valider);
  validerRef.current = valider;

  const recharger = useCallback(async () => {
    try {
      const brut = await api<unknown>("GET", chemin);
      const donnees = validerRef.current(brut);
      setEtat(donnees === null ? { statut: "absent", donnees: null } : { statut: "ok", donnees });
    } catch (e) {
      // Point d'accès pas encore livré, hors ligne ou erreur passagère : repli silencieux (l'écran reste utile).
      setEtat((avant) => (avant.statut === "ok" && !(e instanceof ErreurApi && STATUTS_ABSENT.has(e.statut)) ? avant : { statut: "absent", donnees: null }));
    }
  }, [chemin]);

  useEffect(() => {
    void recharger();
    const surMaj = (e: Event) => {
      const s = (e as CustomEvent).detail?.sujet;
      if (s === sujet || s === "*") void recharger();
    };
    window.addEventListener(EVENEMENT, surMaj);
    return () => window.removeEventListener(EVENEMENT, surMaj);
  }, [sujet, recharger]);


  const remplacer = useCallback((donnees: T) => setEtat({ statut: "ok", donnees }), []);
  return { ...etat, recharger, remplacer };
}

// --- formes attendues ---------------------------------------------------------------------------------------------

export type ArticleCourses = { id?: number | string; article?: string; coche?: boolean; ajoute_par?: string; ts?: string; quantite?: string };
export type RayonCourses = { rayon?: string; articles?: ArticleCourses[] };

/** Rayons d'une liste : `{rayons: [...]}` (comme la carte « courses ») ou `{articles: [...]}` à plat (champ `rayon`). */
export function rayonsDe(brut: unknown): RayonCourses[] | null {
  if (!brut || typeof brut !== "object") return null;
  const o = brut as Record<string, unknown>;
  if (Array.isArray(o.rayons)) return o.rayons as RayonCourses[];
  if (Array.isArray(o.articles)) {
    const parRayon = new Map<string, ArticleCourses[]>();
    for (const a of o.articles as (ArticleCourses & { rayon?: string })[]) {
      const r = a.rayon || "autre";
      if (!parRayon.has(r)) parRayon.set(r, []);
      parRayon.get(r)!.push(a);
    }
    return [...parRayon].map(([rayon, articles]) => ({ rayon, articles }));
  }
  return null;
}

export const listeDe = <T,>(brut: unknown, ...cles: string[]): T[] | null => {
  if (Array.isArray(brut)) return brut as T[];
  if (!brut || typeof brut !== "object") return null;
  for (const c of cles) {
    const v = (brut as Record<string, unknown>)[c];
    if (Array.isArray(v)) return v as T[];
  }
  return null;
};

// Rayons des outils (`tools/courses.py`, identifiants courts) → libellés lisibles ; un rayon déjà lisible est gardé.
const RAYONS: Record<string, string> = {
  "fruits-légumes": "Fruits et légumes",
  "fruits-legumes": "Fruits et légumes",
  boulangerie: "Boulangerie",
  laitier: "Crèmerie",
  "viande-poisson": "Viande et poisson",
  épicerie: "Épicerie",
  epicerie: "Épicerie",
  surgelés: "Surgelés",
  surgeles: "Surgelés",
  boissons: "Boissons",
  hygiène: "Hygiène",
  hygiene: "Hygiène",
  maison: "Maison",
  autre: "Autre",
};

export function libelleRayon(rayon: string | undefined): string {
  const r = (rayon || "").trim();
  if (!r) return "Autre";
  return RAYONS[r.toLowerCase()] ?? r.charAt(0).toUpperCase() + r.slice(1);
}

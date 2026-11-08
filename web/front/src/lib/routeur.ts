// Routage minimal par chemin (/courses, /semaine…) : le fragment reste libre pour le jeton d'invitation (#jeton).
import { useSyncExternalStore } from "react";

export const ECRANS = [
  "conversation",
  "semaine",
  "courses",
  "repas",
  "echeances",
  "journal",
  "fichiers",
  "memoire",
  "routines",
  "reglages",
  "plus",
] as const;
export type Ecran = (typeof ECRANS)[number];

const ecouteurs = new Set<() => void>();
const prevenir = () => ecouteurs.forEach((f) => f());
if (typeof window !== "undefined") window.addEventListener("popstate", prevenir);

export function ecranCourant(): Ecran {
  const nom = location.pathname.replace(/^\/+|\/+$/g, "");
  return (ECRANS as readonly string[]).includes(nom) ? (nom as Ecran) : "conversation";
}

export function useEcran(): Ecran {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    ecranCourant,
  );
}

/** Correspondance des écrans nommés par les cartes (`ouvrir` → `args.ecran`) avec ceux de l'appli. */
const ALIAS: Record<string, Ecran> = { agenda: "semaine", accueil: "conversation", documents: "fichiers", fichier: "fichiers", echeance: "echeances" };

export function naviguer(ecran: string, remplacer = false): void {
  const cible = (ECRANS as readonly string[]).includes(ecran) ? ecran : ALIAS[ecran] ?? "conversation";
  const chemin = cible === "conversation" ? "/" : `/${cible}`;
  if (location.pathname !== chemin || location.hash) {
    if (remplacer) history.replaceState(null, "", chemin);
    else history.pushState(null, "", chemin);
    prevenir();
  }
  window.scrollTo(0, 0);
}

// Palette et thème par personne, mémorisés sur l'appareil (maquette : 8 palettes, système/clair/sombre).
import { useEffect, useSyncExternalStore } from "react";
import { ecrire, lire } from "./stockage";

export const PALETTES = [
  { cle: "zinc", nom: "Zinc", clair: "#18181b", sombre: "#fafafa" },
  { cle: "yellow", nom: "Jaune", clair: "#facc15", sombre: "#facc15" },
  { cle: "red", nom: "Rouge", clair: "#dc2626", sombre: "#ef4444" },
  { cle: "rose", nom: "Rose", clair: "#e11d48", sombre: "#fb7185" },
  { cle: "orange", nom: "Orange", clair: "#ea580c", sombre: "#fb923c" },
  { cle: "green", nom: "Vert", clair: "#16a34a", sombre: "#4ade80" },
  { cle: "blue", nom: "Bleu", clair: "#2563eb", sombre: "#60a5fa" },
  { cle: "violet", nom: "Violet", clair: "#7c3aed", sombre: "#a78bfa" },
] as const;

export type CleTheme = "system" | "light" | "dark";

export interface Preferences {
  palette: string;
  theme: CleTheme;
  vitesseVoix: number;
  voixNavigateur: string;
  vocaliserLive: boolean;
}

/** Palette par défaut stable pour une personne (dérivée de son identifiant, jamais d'un prénom en dur). */
export function paletteParDefaut(linuxUser: string): string {
  let h = 0;
  for (const c of linuxUser) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const choix = PALETTES.filter((p) => p.cle !== "zinc");
  return choix[h % choix.length].cle;
}

let courant: Preferences = { palette: "zinc", theme: "system", vitesseVoix: 1, voixNavigateur: "", vocaliserLive: true };
let cleCourante = "";
const ecouteurs = new Set<() => void>();

export function chargerPreferences(linuxUser: string): void {
  cleCourante = `homeos:preferences:${linuxUser}`;
  courant = { ...courant, palette: paletteParDefaut(linuxUser), ...lire<Partial<Preferences>>(cleCourante, {}) };
  appliquerApparence();
  ecouteurs.forEach((f) => f());
}

export function modifierPreferences(p: Partial<Preferences>): void {
  courant = { ...courant, ...p };
  if (cleCourante) ecrire(cleCourante, courant);
  appliquerApparence();
  ecouteurs.forEach((f) => f());
}

export function preferences(): Preferences {
  return courant;
}

export function usePreferences(): Preferences {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => courant,
  );
}

const requeteSombre = () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null);

export function themeEffectif(): "light" | "dark" {
  if (courant.theme !== "system") return courant.theme;
  return requeteSombre()?.matches ? "dark" : "light";
}

export function appliquerApparence(): void {
  const racine = document.documentElement;
  racine.setAttribute("data-theme", themeEffectif());
  if (courant.palette === "zinc") racine.removeAttribute("data-palette");
  else racine.setAttribute("data-palette", courant.palette);
}

/** Suit le réglage du système quand le thème est « Système ». */
export function useSuiviTheme(): void {
  useEffect(() => {
    const mq = requeteSombre();
    if (!mq) return;
    const f = () => appliquerApparence();
    mq.addEventListener("change", f);
    return () => mq.removeEventListener("change", f);
  }, []);
}

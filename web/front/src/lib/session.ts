// Session courante (personne connectée, prénom de l'assistant lu de la configuration — jamais en dur).
import { useSyncExternalStore } from "react";
import { api } from "./api";
import type { Session } from "./types";

let session: Session = { connecte: false };
const ecouteurs = new Set<() => void>();

export function definirSession(s: Session): void {
  session = s;
  ecouteurs.forEach((f) => f());
}

export function sessionCourante(): Session {
  return session;
}

/** Relit la session (après l'ajout d'une passkey : une session de secours devient une session passkey). */
export async function rafraichirSession(): Promise<void> {
  const s = await api<Session>("GET", "/auth/session");
  if (s.connecte) definirSession(s);
}

export function useSession(): Session {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => session,
  );
}

/** Prénom de l'assistant (configuration du foyer) ; repli neutre tant que la session n'est pas chargée. */
export function useAssistant(): string {
  return useSession().assistant || "l’assistant";
}

/** Couleur d'un agenda : le sien (palette), le commun (gris), celui de l'autre adulte (violet). */
export function couleurAgenda(agenda: unknown): string {
  const a = String(agenda || "").toLowerCase();
  const p = session.personne;
  if (p && (a === p.linux_user.toLowerCase() || a === p.prenom.toLowerCase())) return "var(--primary)";
  if (!a || /famille|foyer|commun|maison/.test(a)) return "var(--c-famille)";
  return "var(--c-autre)";
}

export function nomAgenda(agenda: unknown): string {
  const a = String(agenda || "");
  const p = session.personne;
  if (!a) return "";
  if (p && a.toLowerCase() === p.linux_user.toLowerCase()) return p.prenom;
  return a.charAt(0).toUpperCase() + a.slice(1);
}

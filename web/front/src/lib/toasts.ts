// Messages courts (toasts) : au-dessus du champ de saisie, jamais dessus.
import { useSyncExternalStore } from "react";

export interface Toast {
  id: number;
  texte: string;
}

let liste: Toast[] = [];
let suivant = 1;
const ecouteurs = new Set<() => void>();
const prevenir = () => ecouteurs.forEach((f) => f());

export function toast(texte: string, dureeMs = 3500): void {
  const t = { id: suivant++, texte };
  liste = [...liste, t].slice(-3);
  prevenir();
  setTimeout(() => {
    liste = liste.filter((x) => x.id !== t.id);
    prevenir();
  }, dureeMs);
}

export function useToasts(): Toast[] {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => liste,
  );
}

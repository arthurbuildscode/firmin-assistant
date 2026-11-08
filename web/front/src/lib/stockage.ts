// Préférences propres à cet appareil (palette, thème, voix) : localStorage, toujours entouré de try/catch.
// Jamais de donnée de conversation ni de secret ici.

export function lire<T>(cle: string, defaut: T): T {
  try {
    const brut = window.localStorage.getItem(cle);
    return brut === null ? defaut : (JSON.parse(brut) as T);
  } catch {
    return defaut;
  }
}

export function ecrire(cle: string, valeur: unknown): void {
  try {
    window.localStorage.setItem(cle, JSON.stringify(valeur));
  } catch {
    /* stockage indisponible (navigation privée) : réglage gardé pour la session seulement */
  }
}

export function effacer(cle: string): void {
  try {
    window.localStorage.removeItem(cle);
  } catch {
    /* rien */
  }
}

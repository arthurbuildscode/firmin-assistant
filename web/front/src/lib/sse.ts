// Lecture d'un flux SSE (text/event-stream) depuis une réponse fetch : POST des messages et flux long-vivant.
// L'API EventSource ne convient pas (POST, en-tête CSRF, reprise avec ?apres=) : analyse manuelle, testée (sse.test.ts).

export interface EvenementSSE {
  type: string;
  donnees: unknown;
  id?: string;
}

/** Analyseur incrémental : on lui donne des morceaux de texte, il rend les événements complets. */
export class AnalyseurSSE {
  private tampon = "";

  pousser(morceau: string): EvenementSSE[] {
    this.tampon += morceau;
    const sortie: EvenementSSE[] = [];
    // Fin d'événement : ligne vide (\n\n, \r\n\r\n ou \r\r).
    for (;;) {
      const m = /\r\n\r\n|\n\n|\r\r/.exec(this.tampon);
      if (!m) break;
      const bloc = this.tampon.slice(0, m.index);
      this.tampon = this.tampon.slice(m.index + m[0].length);
      const ev = analyserBloc(bloc);
      if (ev) sortie.push(ev);
    }
    return sortie;
  }

  /** Fin du flux : un dernier événement sans ligne vide finale est rendu s'il est complet. */
  terminer(): EvenementSSE[] {
    const reste = this.tampon;
    this.tampon = "";
    const ev = reste.trim() ? analyserBloc(reste) : null;
    return ev ? [ev] : [];
  }
}

export function analyserBloc(bloc: string): EvenementSSE | null {
  let type = "message";
  let id: string | undefined;
  const donnees: string[] = [];
  for (const ligne of bloc.split(/\r\n|\n|\r/)) {
    if (!ligne || ligne.startsWith(":")) continue; // commentaire (ping de sse-starlette)
    const i = ligne.indexOf(":");
    const champ = i < 0 ? ligne : ligne.slice(0, i);
    let valeur = i < 0 ? "" : ligne.slice(i + 1);
    if (valeur.startsWith(" ")) valeur = valeur.slice(1);
    if (champ === "event") type = valeur;
    else if (champ === "data") donnees.push(valeur);
    else if (champ === "id") id = valeur;
  }
  if (!donnees.length) return null;
  const brut = donnees.join("\n");
  let valeur: unknown = brut;
  try {
    valeur = JSON.parse(brut);
  } catch {
    // texte brut : rendu tel quel
  }
  return { type, donnees: valeur, id };
}

/** Lit toute la réponse et appelle `surEvenement` pour chaque événement ; s'arrête si `signal` est annulé. */
export async function lireSSE(
  reponse: Response,
  surEvenement: (ev: EvenementSSE) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (!reponse.body) return;
  const lecteur = reponse.body.getReader();
  const decodeur = new TextDecoder();
  const analyseur = new AnalyseurSSE();
  try {
    for (;;) {
      if (signal?.aborted) break;
      const { value, done } = await lecteur.read();
      if (done) break;
      for (const ev of analyseur.pousser(decodeur.decode(value, { stream: true }))) surEvenement(ev);
    }
    for (const ev of analyseur.pousser(decodeur.decode())) surEvenement(ev);
    for (const ev of analyseur.terminer()) surEvenement(ev);
  } finally {
    try {
      lecteur.releaseLock();
    } catch {
      /* déjà libéré */
    }
  }
}

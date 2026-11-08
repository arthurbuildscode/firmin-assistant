// Rendu sûr du texte de l'assistant : paragraphes, listes à puces ou numérotées, **gras**, *italique*, liens https.
// Aucun HTML n'est interprété (pas de dangerouslySetInnerHTML).
import type { ReactNode } from "react";

const MOTIF = /(\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*|`[^`]+`|https:\/\/[^\s)]+)/g;

function enLigne(texte: string, cle: string): ReactNode[] {
  const sortie: ReactNode[] = [];
  let dernier = 0;
  let i = 0;
  for (const m of texte.matchAll(MOTIF)) {
    const debut = m.index ?? 0;
    if (debut > dernier) sortie.push(texte.slice(dernier, debut));
    const t = m[0];
    const k = `${cle}-${i++}`;
    if (t.startsWith("**") || t.startsWith("__")) sortie.push(<strong key={k} className="font-semibold">{t.slice(2, -2)}</strong>);
    else if (t.startsWith("`")) sortie.push(<code key={k} className="font-mono text-[.9em]">{t.slice(1, -1)}</code>);
    else if (t.startsWith("https://"))
      sortie.push(
        <a key={k} href={t} target="_blank" rel="noopener noreferrer" className="underline underline-offset-2">
          {t.replace(/^https:\/\//, "").slice(0, 48)}
        </a>,
      );
    else sortie.push(<em key={k}>{t.slice(1, -1)}</em>);
    dernier = debut + t.length;
  }
  if (dernier < texte.length) sortie.push(texte.slice(dernier));
  return sortie;
}

/** Lignes techniques à ne jamais afficher pendant le streaming (retirées par le cœur dans le texte final). */
export function nettoyerFlux(texte: string): string {
  return texte
    .split("\n")
    .filter((l) => !/^\s*`?CARTE:/.test(l) && !/CONFIRMATION_REQUISE:/.test(l))
    .join("\n");
}

export function TexteRiche({ texte }: { texte: string }) {
  const blocs: ReactNode[] = [];
  const lignes = texte.replace(/\r\n/g, "\n").split("\n");
  const etat: { liste: { ordonnee: boolean; items: string[] } | null; paragraphe: string[] } = { liste: null, paragraphe: [] };
  const viderParagraphe = () => {
    if (!etat.paragraphe.length) return;
    const k = `p${blocs.length}`;
    blocs.push(
      <p key={k}>
        {etat.paragraphe.flatMap((l, j) => (j ? [<br key={`${k}-br${j}`} />, ...enLigne(l, `${k}-${j}`)] : enLigne(l, `${k}-${j}`)))}
      </p>,
    );
    etat.paragraphe = [];
  };
  const viderListe = () => {
    const liste = etat.liste;
    if (!liste) return;
    const k = `l${blocs.length}`;
    const Balise = liste.ordonnee ? "ol" : "ul";
    blocs.push(
      <Balise key={k} className={liste.ordonnee ? "list-decimal" : "list-disc"}>
        {liste.items.map((it, j) => (
          <li key={`${k}-${j}`}>{enLigne(it, `${k}-${j}`)}</li>
        ))}
      </Balise>,
    );
    etat.liste = null;
  };
  for (const brute of lignes) {
    const ligne = brute.replace(/^#{1,6}\s+/, "");
    const puce = /^\s*[-*•]\s+(.*)$/.exec(ligne);
    const numero = /^\s*\d+[.)]\s+(.*)$/.exec(ligne);
    if (puce || numero) {
      viderParagraphe();
      const ordonnee = !!numero;
      if (etat.liste && etat.liste.ordonnee !== ordonnee) viderListe();
      if (!etat.liste) etat.liste = { ordonnee, items: [] };
      etat.liste.items.push((puce ?? numero)![1]);
    } else if (!ligne.trim()) {
      viderParagraphe();
      viderListe();
    } else {
      viderListe();
      etat.paragraphe.push(ligne);
    }
  }
  viderParagraphe();
  viderListe();
  return <>{blocs}</>;
}

/** Texte brut pour la lecture à voix haute (sans marques ni puces). */
export function texteALire(texte: string): string {
  return texte
    .replace(/\*\*|__|`/g, "")
    .replace(/^\s*[-*•]\s+/gm, "")
    .replace(/https:\/\/\S+/g, "")
    .replace(/\n{2,}/g, ". ")
    .replace(/\n/g, ", ")
    .trim();
}

// Barre d'outils discrète sous une réponse de l'assistant (inspirée de l'app Claude) : copier, écouter, pouce haut,
// pouce bas (avec « qu'est-ce qui n'allait pas ? » facultatif), réessayer. Icônes seules (info-bulles et aria-label),
// grises ; visibles au survol sur ordinateur, toujours visibles mais légères sur mobile. Rien ne bouge sous le doigt :
// une icône change d'état sur place, le champ du pouce bas s'ouvre en dessous.
import { useState } from "react";
import { Check, Copy, RotateCcw, ThumbsDown, ThumbsUp } from "lucide-react";
import { InfoBulle } from "@/components/ui/tooltip";
import { api, messageErreur } from "@/lib/api";
import { useSession } from "@/lib/session";
import { lire, ecrire } from "@/lib/stockage";
import { texteALire } from "@/lib/texte";
import { toast } from "@/lib/toasts";
import type { MessageFil } from "@/lib/types";
import { magasin, useConversation } from "@/store/conversation";
import { BoutonEcouter } from "./cartes/actions";

type Note = 1 | -1;

function cleAvis(linuxUser: string | undefined) {
  return `homeos:avis:${linuxUser || "?"}`;
}

async function copier(texte: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(texte);
  const zone = document.createElement("textarea");
  zone.value = texte;
  zone.setAttribute("readonly", "");
  zone.style.position = "fixed";
  zone.style.opacity = "0";
  document.body.appendChild(zone);
  zone.select();
  document.execCommand("copy");
  zone.remove();
}

export function BarreReponse({ m, texte, question }: { m: MessageFil; texte: string; question?: MessageFil }) {
  const session = useSession();
  const enCours = useConversation((e) => e.echangeEnCours);
  const cle = cleAvis(session.personne?.linux_user);
  const [note, setNote] = useState<Note | null>(() => (m.id ? (lire<Record<string, Note>>(cle, {})[String(m.id)] ?? null) : null));
  const [copie, setCopie] = useState(false);
  const [commentaire, setCommentaire] = useState<string | null>(null);
  const [envoi, setEnvoi] = useState(false);

  const enregistrer = async (n: Note, texteCommentaire?: string) => {
    if (!m.id) return;
    setEnvoi(true);
    try {
      await api("POST", `/api/messages/${m.id}/avis`, { note: n, ...(texteCommentaire?.trim() ? { commentaire: texteCommentaire.trim() } : {}) });
      const avis = lire<Record<string, Note>>(cle, {});
      ecrire(cle, { ...avis, [String(m.id)]: n });
      setNote(n);
      if (texteCommentaire !== undefined) toast("Merci, c’est noté.");
    } catch (e) {
      toast(messageErreur(e));
    } finally {
      setEnvoi(false);
    }
  };

  const voter = (n: Note) => {
    if (n === 1) {
      setCommentaire(null);
      void enregistrer(1);
    } else {
      setNote(-1);
      setCommentaire("");
      void enregistrer(-1);
    }
  };

  const reessayer = () => {
    if (!question) return;
    void magasin.envoyer(question.texte, { contexte: { ecran: question.ecran ?? "conversation" } });
  };

  return (
    <>
      <div className="barre-reponse" role="toolbar" aria-label="Actions sur la réponse">
        <InfoBulle texte={copie ? "Copié" : "Copier"}>
          <button
            type="button"
            className="bouton-mini"
            aria-label={copie ? "Copié" : "Copier la réponse"}
            onClick={() =>
              copier(texteALire(texte))
                .then(() => {
                  setCopie(true);
                  window.setTimeout(() => setCopie(false), 1500);
                })
                .catch(() => toast("Copie impossible sur ce navigateur."))
            }
          >
            {copie ? <Check /> : <Copy />}
          </button>
        </InfoBulle>
        <BoutonEcouter texte={texte} cle={`reponse-${m.cle}`} className="bouton-mini" variante="mini" />
        {m.id !== undefined && (
          <>
            <InfoBulle texte="Réponse utile">
              <button type="button" className="bouton-mini" aria-label="Réponse utile" aria-pressed={note === 1} disabled={envoi} onClick={() => voter(1)}>
                <ThumbsUp className={note === 1 ? "fill-current" : ""} />
              </button>
            </InfoBulle>
            <InfoBulle texte="Réponse pas utile">
              <button type="button" className="bouton-mini" aria-label="Réponse pas utile" aria-pressed={note === -1} disabled={envoi} onClick={() => voter(-1)}>
                <ThumbsDown className={note === -1 ? "fill-current" : ""} />
              </button>
            </InfoBulle>
          </>
        )}
        {question && (
          <InfoBulle texte="Réessayer">
            <button type="button" className="bouton-mini" aria-label="Réessayer : renvoyer la même demande" disabled={enCours} onClick={reessayer}>
              <RotateCcw />
            </button>
          </InfoBulle>
        )}
      </div>
      {commentaire !== null && (
        <form
          className="avis-commentaire"
          onSubmit={(e) => {
            e.preventDefault();
            const t = commentaire;
            setCommentaire(null);
            if (t.trim()) void enregistrer(-1, t);
          }}
        >
          <input
            className="champ flex-1"
            autoFocus
            value={commentaire}
            maxLength={2000}
            onChange={(e) => setCommentaire(e.target.value)}
            placeholder="Qu’est-ce qui n’allait pas ? (facultatif)"
            aria-label="Qu’est-ce qui n’allait pas ? (facultatif)"
          />
          <button type="submit" className="bouton-mini plein" aria-label="Envoyer le commentaire">
            <Check />
          </button>
        </form>
      )}
    </>
  );
}

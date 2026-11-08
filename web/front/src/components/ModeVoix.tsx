// Mode voix live, plein écran : écoute (fin de parole détectée) → transcription sur le serveur → échange normal de
// la conversation → réponse lue (Piper, sinon synthèse du navigateur) → nouvelle écoute. Les échanges restent dans le fil.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Mic, Phone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { messageErreur } from "@/lib/api";
import { preferences } from "@/lib/preferences";
import { useAssistant } from "@/lib/session";
import { toast } from "@/lib/toasts";
import { arreterLecture, Enregistreur, lireAVoixHaute, microDisponible, transcrire } from "@/lib/voix";
import { magasin, useConversation } from "@/store/conversation";

type Phase = "ecoute" | "reflexion" | "parle" | "pause";

export function ModeVoix({ ecran, onFermer }: { ecran: string; onFermer: () => void }) {
  const assistant = useAssistant();
  const [phase, setPhase] = useState<Phase>("ecoute");
  const [dit, setDit] = useState("");
  const [reponse, setReponse] = useState("");
  const [echanges, setEchanges] = useState(0);
  const actif = useRef(true);
  const enregistreur = useRef<Enregistreur | null>(null);
  const silencesVides = useRef(0);
  const etape = useConversation((e) => {
    const cle = e.dernierEchange?.assistant;
    const m = cle ? e.messages.find((x) => x.cle === cle) : undefined;
    return m?.enCours ? m.etapes[m.etapes.length - 1]?.etape ?? "Je réfléchis…" : "";
  });

  const ecouter = async () => {
    if (!actif.current) return;
    setPhase("ecoute");
    setDit("");
    const e = new Enregistreur();
    enregistreur.current = e;
    let audio: Blob;
    try {
      audio = await (await e.demarrer({ finDeParole: true, silenceMs: 1300, dureeMaxMs: 30000 }));
    } catch (err) {
      toast(err instanceof DOMException ? "Accès au micro refusé." : messageErreur(err));
      fermer();
      return;
    }
    if (!actif.current) return;
    if (!e.aParle) {
      silencesVides.current += 1;
      if (silencesVides.current >= 2) setPhase("pause");
      else void ecouter();
      return;
    }
    silencesVides.current = 0;
    setPhase("reflexion");
    try {
      const texte = await transcrire(audio);
      if (!actif.current) return;
      if (!texte) {
        void ecouter();
        return;
      }
      setDit(texte);
      const m = await magasin.envoyer(texte, { voix: true, contexte: { ecran } });
      if (!actif.current) return;
      setEchanges((n) => n + 1);
      const dire = m?.erreur || m?.texte || "C’est fait.";
      setReponse(dire);
      setPhase("parle");
      if (preferences().vocaliserLive) await lireAVoixHaute(dire, "voix-live").catch((err) => toast(messageErreur(err)));
      else await new Promise((ok) => setTimeout(ok, Math.min(8000, 1400 + dire.length * 45)));
      if (actif.current) void ecouter();
    } catch (err) {
      toast(messageErreur(err));
      if (actif.current) setPhase("pause");
    }
  };

  const fermer = () => {
    actif.current = false;
    enregistreur.current?.annuler();
    arreterLecture();
    onFermer();
  };

  useEffect(() => {
    if (!microDisponible()) {
      toast("Micro indisponible sur ce navigateur.");
      onFermer();
      return;
    }
    actif.current = true;
    void ecouter();
    const echap = (e: KeyboardEvent) => e.key === "Escape" && fermer();
    window.addEventListener("keydown", echap);
    return () => {
      window.removeEventListener("keydown", echap);
      actif.current = false;
      enregistreur.current?.annuler();
      arreterLecture();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const statut = { ecoute: "J’écoute…", reflexion: "Je réfléchis…", parle: `${assistant} parle`, pause: "À toi quand tu veux" }[phase];
  return createPortal(
    <div className="voix-live" role="dialog" aria-modal="true" aria-label={`Mode voix avec ${assistant}`}>
      <div className="voix-haut">
        <Badge variant="outline">
          <span className="rec" aria-hidden />
          Mode voix · {echanges} échange{echanges > 1 ? "s" : ""}
        </Badge>
      </div>
      <div className={`orbe ${phase}`} aria-hidden />
      <div className="text-sm text-muted-foreground" role="status">
        {statut}
      </div>
      {phase === "reflexion" && etape && <div className="pulsation text-base">{etape}</div>}
      <div className="min-h-[1.6em] max-w-[520px] text-base italic text-muted-foreground">{dit ? `« ${dit} »` : ""}</div>
      <div className="voix-dit" aria-live="polite">
        {phase === "parle" ? reponse : ""}
      </div>
      <div className="mt-2 flex flex-wrap justify-center gap-3">
        {phase === "parle" && (
          <Button
            size="lg"
            className="h-12 rounded-full px-5"
            onClick={() => {
              arreterLecture();
            }}
          >
            <Mic />
            Interrompre
          </Button>
        )}
        {phase === "pause" && (
          <Button size="lg" className="h-12 rounded-full px-5" onClick={() => void ecouter()}>
            <Mic />
            Parler
          </Button>
        )}
        <Button size="lg" variant="destructive" className="h-12 rounded-full px-5" onClick={fermer}>
          <Phone />
          Raccrocher
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">Les échanges restent dans la conversation.</p>
    </div>,
    document.body,
  );
}

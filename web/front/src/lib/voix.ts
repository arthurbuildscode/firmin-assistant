// Voix : lecture à voix haute (Piper sur le serveur, sinon synthèse du navigateur), dictée (MediaRecorder →
// faster-whisper sur le serveur) et détection de fin de parole pour le mode voix live.
import { useSyncExternalStore } from "react";
import { lireErreur, requete, requeteRessayee } from "./api";
import { preferences } from "./preferences";
import { texteALire } from "./texte";

// --- lecture -----------------------------------------------------------------------------------------------------------

let lectureCourante: { cle: string; arreter: () => void } | null = null;
const ecouteurs = new Set<() => void>();
const prevenir = () => ecouteurs.forEach((f) => f());

/** Clé de ce qui est en train d'être lu (pour l'icône haut-parleur / stop), ou null. */
export function useLecture(): string | null {
  return useSyncExternalStore(
    (f) => {
      ecouteurs.add(f);
      return () => ecouteurs.delete(f);
    },
    () => lectureCourante?.cle ?? null,
  );
}

export function arreterLecture(): void {
  const l = lectureCourante;
  lectureCourante = null;
  l?.arreter();
  prevenir();
}

export function voixNavigateurDisponibles(): SpeechSynthesisVoice[] {
  try {
    return window.speechSynthesis?.getVoices().filter((v) => v.lang.toLowerCase().startsWith("fr")) ?? [];
  } catch {
    return [];
  }
}

function lireNavigateur(texte: string, fin: () => void): () => void {
  const synth = window.speechSynthesis;
  if (!synth || typeof SpeechSynthesisUtterance === "undefined") throw new Error("Lecture vocale indisponible sur cet appareil.");
  const u = new SpeechSynthesisUtterance(texte);
  u.lang = "fr-FR";
  u.rate = preferences().vitesseVoix || 1;
  const voix = voixNavigateurDisponibles();
  const choisie = voix.find((v) => v.name === preferences().voixNavigateur) ?? voix[0];
  if (choisie) u.voice = choisie;
  u.onend = fin;
  u.onerror = fin;
  synth.cancel();
  synth.speak(u);
  return () => synth.cancel();
}

/** Lit un texte ; se résout quand la lecture est finie (ou arrêtée). Lève une erreur si aucune voix n'est disponible. */
export async function lireAVoixHaute(texteBrut: string, cle = "lecture"): Promise<void> {
  arreterLecture();
  const texte = texteALire(texteBrut).slice(0, 4000);
  if (!texte) return;
  let resoudre: () => void = () => {};
  const fini = new Promise<void>((r) => (resoudre = r));
  const terminer = () => {
    if (lectureCourante?.cle === cle) {
      lectureCourante = null;
      prevenir();
    }
    resoudre();
  };
  lectureCourante = { cle, arreter: terminer };
  prevenir();
  let r: Response | null = null;
  try {
    r = await requete("POST", "/api/voix/synthese", { texte });
  } catch {
    r = null;
  }
  if (lectureCourante?.cle !== cle) return;
  if (r && r.ok) {
    const url = URL.createObjectURL(await r.blob());
    const audio = new Audio(url);
    audio.playbackRate = preferences().vitesseVoix || 1;
    const nettoyer = () => URL.revokeObjectURL(url);
    audio.onended = () => {
      nettoyer();
      terminer();
    };
    audio.onerror = () => {
      nettoyer();
      terminer();
    };
    lectureCourante = {
      cle,
      arreter: () => {
        audio.pause();
        nettoyer();
        terminer();
      },
    };
    try {
      await audio.play();
    } catch {
      terminer();
    }
    return fini;
  }
  try {
    const arret = lireNavigateur(texte, terminer);
    lectureCourante = { cle, arreter: () => { arret(); terminer(); } };
  } catch (e) {
    terminer();
    throw e;
  }
  return fini;
}

// --- dictée --------------------------------------------------------------------------------------------------------------

export const microDisponible = (): boolean =>
  typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";

function typeAudio(): string {
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]) {
    try {
      if (MediaRecorder.isTypeSupported(t)) return t;
    } catch {
      /* navigateur ancien */
    }
  }
  return "";
}

export interface OptionsEnregistrement {
  /** Arrêt automatique après un silence suivant de la parole (mode voix live). */
  finDeParole?: boolean;
  silenceMs?: number;
  dureeMaxMs?: number;
  surNiveau?: (niveau: number) => void;
}

export class Enregistreur {
  private flux: MediaStream | null = null;
  private enregistreur: MediaRecorder | null = null;
  private morceaux: Blob[] = [];
  private contexteAudio: AudioContext | null = null;
  private minuterie: number | null = null;
  private arreteResoudre: ((b: Blob) => void) | null = null;
  debut = 0;
  aParle = false;

  async demarrer(options: OptionsEnregistrement = {}): Promise<Promise<Blob>> {
    this.flux = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const type = typeAudio();
    this.enregistreur = new MediaRecorder(this.flux, type ? { mimeType: type, audioBitsPerSecond: 32000 } : undefined);
    this.morceaux = [];
    this.enregistreur.ondataavailable = (e) => e.data.size && this.morceaux.push(e.data);
    const fini = new Promise<Blob>((r) => (this.arreteResoudre = r));
    this.enregistreur.onstop = () => {
      this.liberer();
      this.arreteResoudre?.(new Blob(this.morceaux, { type: this.enregistreur?.mimeType || "audio/webm" }));
    };
    this.enregistreur.start(1000);
    this.debut = Date.now();
    if (options.finDeParole || options.surNiveau) this.surveiller(options);
    return fini;
  }

  private surveiller(o: OptionsEnregistrement): void {
    try {
      const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.contexteAudio = new Ctx();
      const source = this.contexteAudio.createMediaStreamSource(this.flux!);
      const analyseur = this.contexteAudio.createAnalyser();
      analyseur.fftSize = 1024;
      source.connect(analyseur);
      const tampon = new Uint8Array(analyseur.fftSize);
      let dernierSon = Date.now();
      const silence = o.silenceMs ?? 1300;
      const max = o.dureeMaxMs ?? 30000;
      this.minuterie = window.setInterval(() => {
        analyseur.getByteTimeDomainData(tampon);
        let somme = 0;
        for (const v of tampon) somme += ((v - 128) / 128) ** 2;
        const niveau = Math.sqrt(somme / tampon.length);
        o.surNiveau?.(niveau);
        if (niveau > 0.035) {
          dernierSon = Date.now();
          this.aParle = true;
        }
        if (!o.finDeParole) return;
        const ecoule = Date.now() - this.debut;
        if ((this.aParle && Date.now() - dernierSon > silence) || ecoule > max) this.arreter();
      }, 100);
    } catch {
      /* pas d'analyse : arrêt manuel seulement */
    }
  }

  arreter(): void {
    if (this.enregistreur && this.enregistreur.state !== "inactive") this.enregistreur.stop();
    else this.liberer();
  }

  annuler(): void {
    this.arreteResoudre = null;
    this.arreter();
  }

  private liberer(): void {
    if (this.minuterie) window.clearInterval(this.minuterie);
    this.minuterie = null;
    this.contexteAudio?.close().catch(() => {});
    this.contexteAudio = null;
    this.flux?.getTracks().forEach((t) => t.stop());
    this.flux = null;
  }
}

export async function transcrire(audio: Blob): Promise<string> {
  const ext = audio.type.includes("mp4") ? "m4a" : audio.type.includes("ogg") ? "ogg" : "webm";
  const fd = new FormData();
  fd.append("audio", audio, `vocal.${ext}`);
  const r = await requeteRessayee("POST", "/api/voix/transcrire", fd);
  if (!r.ok) throw await lireErreur(r);
  const { texte } = (await r.json()) as { texte: string };
  return (texte || "").trim();
}

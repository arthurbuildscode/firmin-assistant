// Accès à l'API (CONTRACT §15) : en-tête CSRF sur toute requête, jeton de step-up en mémoire seulement (jamais stocké),
// preuve d'identité redemandée automatiquement quand le serveur répond 403 {step_up: true}.
import { credentialJson, optionsAssertion, optionsCreation, nomAppareil, sortiePrf, versB64 } from "./webauthn";

export class ErreurApi extends Error {
  statut: number;
  stepUp: boolean;
  constructor(message: string, statut: number, stepUp = false) {
    super(message);
    this.statut = statut;
    this.stepUp = stepUp;
  }
}

type Ecouteur = () => void;
let jetonStepUp: { jeton: string; expire: number } | null = null;
const surDeconnexion = new Set<Ecouteur>();

/** Session fermée (401, ou événement `deconnecte` du flux) : retour à l'écran de connexion. */
export function signalerDeconnexion(): void {
  jetonStepUp = null;
  surDeconnexion.forEach((fn) => fn());
}

export function quandDeconnecte(fn: Ecouteur): () => void {
  surDeconnexion.add(fn);
  return () => surDeconnexion.delete(fn);
}

export function stepUpValide(): boolean {
  return !!jetonStepUp && jetonStepUp.expire > Date.now() + 5000;
}

export function oublierStepUp(): void {
  jetonStepUp = null;
}

function entetes(json: boolean): Record<string, string> {
  const h: Record<string, string> = { "X-Requested-With": "homeos" };
  if (json) h["Content-Type"] = "application/json";
  if (stepUpValide()) h["X-Homeos-Step-Up"] = jetonStepUp!.jeton;
  return h;
}

async function erreurDe(r: Response): Promise<ErreurApi> {
  const corps = await r.json().catch(() => ({}) as Record<string, unknown>);
  // Écriture refusée en connexion de secours (code Telegram) : message doux plutôt que l'erreur brute.
  const detail = (corps as { detail?: unknown }).detail;
  const objet = detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
  const secours = r.status === 403 && !!((corps as { secours?: unknown }).secours || objet?.secours);
  const fourni = objet ? objet.message : detail ?? (corps as { message?: unknown }).message;
  const message =
    typeof fourni === "string" && fourni
      ? fourni
      : secours
        ? "Connexion de secours : tu peux lire, pas modifier. Ajoute une passkey pour retrouver toutes les actions."
        : `Erreur ${r.status}`;
  return new ErreurApi(message, r.status, !!(corps as { step_up?: unknown }).step_up);
}

/** Requête brute (réponse non lue : SSE, audio). */
export async function requete(methode: string, chemin: string, corps?: unknown, signal?: AbortSignal): Promise<Response> {
  const formulaire = typeof FormData !== "undefined" && corps instanceof FormData;
  const init: RequestInit = { method: methode, headers: entetes(corps !== undefined && !formulaire), credentials: "same-origin", signal };
  if (corps !== undefined) init.body = formulaire ? (corps as FormData) : JSON.stringify(corps);
  const r = await fetch(chemin, init);
  if (r.status === 401 && !chemin.startsWith("/auth/")) signalerDeconnexion();
  return r;
}

const pause = (ms: number) => new Promise((ok) => setTimeout(ok, ms));

/** Comme `requete`, mais un 429 (limites de débit du serveur) est réessayé en douceur avant d'abandonner. */
export async function requeteRessayee(methode: string, chemin: string, corps?: unknown, signal?: AbortSignal): Promise<Response> {
  let r = await requete(methode, chemin, corps, signal);
  for (const attente of [1500, 4000, 8000]) {
    if (r.status !== 429 || signal?.aborted) break;
    await pause(attente);
    r = await requete(methode, chemin, corps, signal);
  }
  return r;
}

export const MESSAGE_429 = "Un instant : je termine ce qui est en cours, réessaie dans quelques secondes.";

/** Requête JSON ; sur 403 step-up, demande la preuve d'identité puis rejoue une fois. */
export async function api<T = unknown>(methode: string, chemin: string, corps?: unknown): Promise<T> {
  let r = await requeteRessayee(methode, chemin, corps);
  if (r.status === 403) {
    const err = await erreurDe(r);
    if (!err.stepUp) throw err;
    // Jeton expiré ou déjà consommé (step-up à usage unique) : nouvelle preuve, puis un seul nouvel essai.
    oublierStepUp();
    await confirmerIdentite();
    r = await requeteRessayee(methode, chemin, corps);
  }
  if (r.status === 429) throw new ErreurApi(MESSAGE_429, 429);
  if (!r.ok) throw await erreurDe(r);
  return (await r.json().catch(() => ({}))) as T;
}

export async function lireErreur(r: Response): Promise<ErreurApi> {
  return erreurDe(r);
}

// --- passkeys --------------------------------------------------------------------------------------------------------

interface Ceremonie {
  ceremonie: string;
  options: unknown;
}

export async function inscrire(jeton: string): Promise<void> {
  const { ceremonie, options } = await api<Ceremonie>("POST", "/auth/inscription/options", jeton ? { jeton } : {});
  // Extension PRF annoncée (documents sensibles) : une passkey qui sait faire pourra protéger la clé des documents.
  const publicKey = { ...optionsCreation(options), extensions: { ...((options as { extensions?: object }).extensions ?? {}), prf: {} } } as PublicKeyCredentialCreationOptions;
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new ErreurApi("Création de la passkey annulée.", 0);
  await api("POST", "/auth/inscription/verification", { ceremonie, credential: credentialJson(cred), nom: nomAppareil() });
}

export async function connecter(): Promise<void> {
  const { ceremonie, options } = await api<Ceremonie>("POST", "/auth/connexion/options", {});
  const cred = (await navigator.credentials.get({ publicKey: optionsAssertion(options) })) as PublicKeyCredential | null;
  if (!cred) throw new ErreurApi("Connexion annulée.", 0);
  await api("POST", "/auth/connexion/verification", { ceremonie, credential: credentialJson(cred) });
}

let stepUpEnCours: Promise<void> | null = null;

/** Step-up : nouvelle assertion avec vérification de l'utilisateur (empreinte, visage, code de l'appareil). */
export function confirmerIdentite(): Promise<void> {
  if (stepUpValide()) return Promise.resolve();
  if (!stepUpEnCours) {
    stepUpEnCours = (async () => {
      const { ceremonie, options } = await api<Ceremonie>("POST", "/auth/step-up/options", {});
      const cred = (await navigator.credentials.get({ publicKey: optionsAssertion(options) })) as PublicKeyCredential | null;
      if (!cred) throw new ErreurApi("Vérification annulée.", 0);
      const r = await api<{ jeton: string; expire: string }>("POST", "/auth/step-up", { ceremonie, credential: credentialJson(cred) });
      jetonStepUp = { jeton: r.jeton, expire: Date.parse(r.expire) || Date.now() + 4 * 60_000 };
    })().finally(() => {
      stepUpEnCours = null;
    });
  }
  return stepUpEnCours;
}

export interface ResultatStepUpPrf {
  /** `rawId` de la passkey utilisée (base64url). */
  credentialId: string;
  /** Sortie PRF (32 octets), gardée en mémoire par l'appelant le temps d'en dériver une clé, puis effacée. */
  prf: Uint8Array<ArrayBuffer> | null;
}

/**
 * Step-up avec l'extension WebAuthn PRF (documents sensibles, docs/documents-sensibles.md §2a) : toujours une nouvelle
 * assertion (il faut la sortie PRF), qui renouvelle aussi le jeton de step-up. `entreePrf` reçoit les identifiants des
 * passkeys autorisées (base64url) et rend l'entrée `prf` (`eval`, `evalByCredential`) ou null. La sortie PRF est
 * retirée de la réponse envoyée au serveur (`credentialJson`).
 */
export async function confirmerIdentitePrf(entreePrf: (autorises: string[]) => Record<string, unknown> | null): Promise<ResultatStepUpPrf> {
  const { ceremonie, options } = await api<Ceremonie>("POST", "/auth/step-up/options", {});
  const autorises = (((options as { allowCredentials?: { id: string }[] }).allowCredentials) ?? []).map((c) => String(c.id));
  const publicKey = optionsAssertion(options);
  const prf = entreePrf(autorises);
  if (prf) publicKey.extensions = { ...(publicKey.extensions ?? {}), prf } as AuthenticationExtensionsClientInputs;
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new ErreurApi("Vérification annulée.", 0);
  const sortie = sortiePrf(cred);
  try {
    const r = await api<{ jeton: string; expire: string }>("POST", "/auth/step-up", { ceremonie, credential: credentialJson(cred) });
    jetonStepUp = { jeton: r.jeton, expire: Date.parse(r.expire) || Date.now() + 4 * 60_000 };
  } catch (e) {
    sortie?.fill(0);
    throw e;
  }
  return { credentialId: versB64(cred.rawId), prf: sortie };
}

export function messageErreur(e: unknown): string {
  if (e instanceof ErreurApi) return e.message;
  if (e instanceof DOMException && (e.name === "NotAllowedError" || e.name === "AbortError")) return "Opération annulée.";
  if (e instanceof TypeError) return "Serveur injoignable : vérifie la connexion (Tailscale).";
  return e instanceof Error ? e.message : String(e);
}

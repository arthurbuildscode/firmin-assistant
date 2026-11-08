// Types partagés : contrat des cartes (CONTRACT §14) et de l'API (§15).

export type NomAction =
  | "message"
  | "confirmer"
  | "ouvrir"
  | "ecouter"
  | "coffre"
  | "coffre_ecrire"
  | "deverrouiller"
  | "routine"
  | "lien";

export interface ActionCarte {
  libelle: string;
  action: NomAction | string;
  args?: Record<string, unknown>;
  /** id d'une action en attente (POST /api/confirmations/{id}) ou « step-up » (preuve d'identité exigée). */
  confirmation?: number | "step-up" | null;
}

export interface Carte {
  type: string;
  id: string;
  titre?: string | null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  donnees: Record<string, any>;
  actions: ActionCarte[];
}

export interface Etape {
  etape: string;
  outil?: string;
  debut?: number;
  fin?: number;
  duree_ms?: number;
  erreur?: boolean;
}

export interface Confirmation {
  id: number;
  resume: string;
  auto?: boolean;
  resultat?: string;
  step_up?: boolean;
}

export interface Masquage {
  texte: string;
  types?: string[];
}

export type Role = "personne" | "assistant" | "systeme";
export type Decision = "valider" | "annuler";

/** Un message du fil (historique ou échange en cours). */
export interface MessageFil {
  cle: string;
  id?: number;
  role: Role;
  texte: string;
  ts: string;
  canal?: string;
  auteur?: string | null;
  cartes: Carte[];
  etapes: Etape[];
  confirmations: Confirmation[];
  dureeMs?: number;
  masquage?: Masquage;
  ecran?: string;
  pieces?: { nom: string }[];
  enCours?: boolean;
  debutMs?: number;
  attente?: string;
  erreur?: string;
  voix?: boolean;
  /** message système né d'une décision (meta.confirmation) */
  decisionDe?: number;
  /** message spontané (brief, routine : `meta.spontane`, canal `routine`) */
  spontane?: boolean;
}

export interface Personne {
  prenom: string;
  linux_user: string;
  role: string;
}

export interface Session {
  connecte: boolean;
  personne?: Personne;
  assistant?: string;
  step_up?: boolean;
  passkeys?: number;
  /** « secours » : session ouverte par le code Telegram (24 h ; converser, lire, ajouter une passkey). */
  niveau?: "passkey" | "secours";
  /** Compte Tailscale auquel la session est liée (en-tête posé par tailscale serve). */
  tailscale?: string | null;
}

export interface MessageApi {
  id: number;
  conversation_id: number;
  ts: string;
  role: Role;
  canal: string;
  auteur?: string | null;
  texte: string;
  cartes: Carte[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  meta: Record<string, any>;
}

export interface ConversationApi {
  id: number;
  titre: string;
  cree: string;
  maj: string;
  nb_messages?: number;
}

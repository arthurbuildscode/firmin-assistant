// État de la conversation : historique, échange en cours (SSE du POST), flux long-vivant (GET …/flux?apres=),
// décisions des confirmations, cartes du jour. Un seul magasin pour tous les écrans : les écrans dédiés lisent les
// cartes produites par les mêmes outils que la conversation.
import { useSyncExternalStore } from "react";
import { api, ErreurApi, lireErreur, MESSAGE_429, messageErreur, requete, requeteRessayee, signalerDeconnexion } from "@/lib/api";
import { lireSSE, type EvenementSSE } from "@/lib/sse";
import { isoJour } from "@/lib/dates";
import { signalerMaj, sujetDeCarte, type Sujet } from "@/lib/donnees";
import { toast } from "@/lib/toasts";
import type { Carte, Confirmation, ConversationApi, Decision, Etape, MessageApi, MessageFil } from "@/lib/types";

export interface Contexte {
  ecran: string;
  objet?: string;
}

export interface EtatConversation {
  conversationId: number | null;
  conversations: ConversationApi[];
  messages: MessageFil[];
  charge: boolean;
  horsLigne: boolean;
  erreurChargement: string | null;
  decisions: Record<number, Decision | "en_cours">;
  echangeEnCours: boolean;
  /** Écran depuis lequel le dernier échange a été lancé (réponse affichée sur cet écran). */
  dernierEcran: string | null;
  dernierEchange: { personne: string; assistant: string } | null;
  accueil: Carte[];
  accueilDate: string | null;
  accueilChargement: boolean;
  /** Messages spontanés (brief, routines) arrivés pendant qu'un autre écran était ouvert : pastille sur Conversation. */
  nonLus: number;
}

const vide = (): EtatConversation => ({
  conversationId: null,
  conversations: [],
  messages: [],
  charge: false,
  horsLigne: typeof navigator !== "undefined" ? !navigator.onLine : false,
  erreurChargement: null,
  decisions: {},
  echangeEnCours: false,
  dernierEcran: null,
  dernierEchange: null,
  accueil: [],
  accueilDate: null,
  accueilChargement: false,
  nonLus: 0,
});

export function depuisApi(m: MessageApi): MessageFil {
  const meta = m.meta || {};
  return {
    cle: `m${m.id}`,
    id: m.id,
    role: m.role,
    texte: m.texte || "",
    ts: m.ts,
    canal: m.canal,
    auteur: m.auteur,
    cartes: Array.isArray(m.cartes) ? m.cartes : [],
    etapes: Array.isArray(meta.etapes) ? meta.etapes : [],
    confirmations: Array.isArray(meta.confirmations) ? meta.confirmations : [],
    dureeMs: typeof meta.duree_ms === "number" ? meta.duree_ms : undefined,
    masquage: meta.masquage && typeof meta.masquage.texte === "string" ? meta.masquage : undefined,
    pieces: Array.isArray(meta.pieces) ? meta.pieces : undefined,
    decisionDe: typeof meta.confirmation === "number" ? meta.confirmation : undefined,
    spontane: meta.spontane === true || m.canal === "routine" ? true : undefined,
  };
}

let compteur = 0;
const nouvelleCle = (p: string) => `${p}-${Date.now().toString(36)}-${++compteur}`;

export class MagasinConversation {
  etat: EtatConversation = vide();
  private ecouteurs = new Set<() => void>();
  private fluxControleur: AbortController | null = null;
  private assistantsEnAttente = new Map<number, MessageFil>();
  private actif = false;
  private demarrage: Promise<void> | null = null;

  // --- abonnement ---------------------------------------------------------------------------------------------------

  abonner = (f: () => void) => {
    this.ecouteurs.add(f);
    return () => this.ecouteurs.delete(f);
  };

  private maj(p: Partial<EtatConversation>): void {
    this.etat = { ...this.etat, ...p };
    this.ecouteurs.forEach((f) => f());
  }

  private majMessage(cle: string, f: (m: MessageFil) => MessageFil): void {
    this.maj({ messages: this.etat.messages.map((m) => (m.cle === cle ? f(m) : m)) });
  }

  private message(cle: string): MessageFil | undefined {
    return this.etat.messages.find((m) => m.cle === cle);
  }

  dernierId(): number {
    return this.etat.messages.reduce((max, m) => Math.max(max, m.id ?? 0), 0);
  }

  // --- démarrage ----------------------------------------------------------------------------------------------------

  demarrer(): Promise<void> {
    // Un seul démarrage à la fois : un message envoyé pendant le chargement attend la conversation.
    if (!this.demarrage) this.demarrage = this.demarrerUneFois().finally(() => (this.demarrage = null));
    return this.demarrage;
  }

  private async demarrerUneFois(): Promise<void> {
    this.actif = true;
    try {
      const d = await api<{ courante: number | null; conversations: ConversationApi[] }>("GET", "/api/conversations");
      let id = d.courante ?? d.conversations[0]?.id ?? null;
      if (id === null) {
        const n = await api<{ conversation: ConversationApi }>("POST", "/api/conversations");
        id = n.conversation.id;
      }
      this.maj({ conversations: d.conversations });
      await this.ouvrir(id);
    } catch (e) {
      this.maj({ charge: true, erreurChargement: messageErreur(e), horsLigne: !navigator.onLine });
    }
  }

  arreter(): void {
    this.actif = false;
    this.fluxControleur?.abort();
    this.fluxControleur = null;
    this.etat = vide();
  }

  async ouvrir(id: number): Promise<void> {
    this.fluxControleur?.abort();
    this.maj({ conversationId: id, messages: [], charge: false, decisions: {}, erreurChargement: null });
    const messages: MessageFil[] = [];
    let apres = 0;
    for (let page = 0; page < 20; page++) {
      const d = await api<{ messages: MessageApi[] }>("GET", `/api/conversations/${id}/messages?apres=${apres}`);
      messages.push(...d.messages.map(depuisApi));
      if (d.messages.length < 200) break;
      apres = d.messages[d.messages.length - 1].id;
    }
    const decisions: Record<number, Decision> = {};
    for (const m of messages) if (m.decisionDe) decisions[m.decisionDe] = /annul/i.test(m.texte) ? "annuler" : "valider";
    this.maj({ messages, charge: true, decisions, horsLigne: !navigator.onLine });
    void this.suivreFlux(id);
  }

  definirHorsLigne(v: boolean): void {
    if (this.etat.horsLigne !== v) this.maj({ horsLigne: v });
  }

  async nouvelleConversation(): Promise<void> {
    const n = await api<{ conversation: ConversationApi }>("POST", "/api/conversations");
    await this.ouvrir(n.conversation.id);
    void this.listerConversations();
  }

  async listerConversations(): Promise<void> {
    try {
      const d = await api<{ conversations: ConversationApi[] }>("GET", "/api/conversations");
      this.maj({ conversations: d.conversations });
    } catch {
      /* hors ligne */
    }
  }

  // --- cartes du jour (sans le modèle) ------------------------------------------------------------------------------

  async chargerAccueil(date?: string): Promise<void> {
    this.maj({ accueilChargement: true });
    try {
      const d = await api<{ date: string; cartes: Carte[] }>("GET", `/api/brief/jour${date ? `?date=${date}` : ""}`);
      this.maj({ accueil: d.cartes, accueilDate: d.date, accueilChargement: false });
    } catch (e) {
      this.maj({ accueilChargement: false });
      if (date) toast(messageErreur(e));
    }
  }

  /** Feuilletage de la carte du jour à l'intérieur d'un message (flèches de la carte bonjour). */
  async changerJourDansMessage(cleMessage: string, idCarte: string, date: string): Promise<void> {
    const d = await api<{ date: string; cartes: Carte[] }>("GET", `/api/brief/jour?date=${date}`);
    this.majMessage(cleMessage, (m) => {
      const i = m.cartes.findIndex((c) => c.id === idCarte);
      if (i < 0) return m;
      // Le bonjour et l'« À noter » du jour affiché sont remplacés ensemble ; le reste du message ne bouge pas.
      const autres = m.cartes.filter((c, j) => j !== i && !(c.type === "a_noter" || c.type === "brief"));
      const avant = autres.filter((c) => m.cartes.indexOf(c) < i);
      const apres = autres.filter((c) => m.cartes.indexOf(c) > i);
      return { ...m, cartes: [...avant, ...d.cartes, ...apres] };
    });
  }

  // --- échange ------------------------------------------------------------------------------------------------------

  async envoyer(texte: string, options: { pieces?: { id: string; nom: string }[]; contexte?: Contexte; voix?: boolean } = {}): Promise<MessageFil | null> {
    texte = texte.trim();
    const pieces = options.pieces ?? [];
    if (!texte && !pieces.length) return null;
    if (this.etat.echangeEnCours) {
      toast("Une demande à la fois : j’attends la fin de la réponse en cours.");
      return null;
    }
    if (!navigator.onLine) {
      toast("Hors ligne : message non envoyé.");
      return null;
    }
    if (this.demarrage) await this.demarrage;
    let convId = this.etat.conversationId;
    if (convId === null) {
      await this.demarrer();
      convId = this.etat.conversationId;
      if (convId === null) return null;
    }
    const maintenant = new Date().toISOString();
    const p: MessageFil = {
      cle: nouvelleCle("p"), role: "personne", texte: texte || pieces.map((x) => x.nom).join(", "), ts: maintenant, cartes: [], etapes: [],
      confirmations: [], ecran: options.contexte?.ecran, pieces: pieces.map((x) => ({ nom: x.nom })), voix: options.voix,
    };
    const a: MessageFil = { cle: nouvelleCle("a"), role: "assistant", texte: "", ts: maintenant, cartes: [], etapes: [], confirmations: [], enCours: true, debutMs: Date.now() };
    this.maj({
      messages: [...this.etat.messages, p, a],
      echangeEnCours: true,
      dernierEcran: options.contexte?.ecran ?? "conversation",
      dernierEchange: { personne: p.cle, assistant: a.cle },
    });
    try {
      const r = await requeteRessayee("POST", `/api/conversations/${convId}/messages`, {
        texte,
        pieces_jointes: pieces.map((x) => x.id),
        contexte: options.contexte ?? { ecran: "conversation" },
      });
      if (r.status === 429) throw new ErreurApi(MESSAGE_429, 429);
      if (!r.ok) throw await lireErreur(r);
      await lireSSE(r, (ev) => this.surEchange(ev, p.cle, a.cle));
    } catch (e) {
      this.majMessage(a.cle, (m) => ({ ...m, enCours: false, erreur: messageErreur(e) }));
    } finally {
      const fin = this.message(a.cle);
      if (fin?.enCours) {
        // Flux coupé avant la fin : l'échange continue sur le serveur, la réponse arrivera par le flux long-vivant.
        const enAttente = [...this.assistantsEnAttente.values()].find((m) => (m.id ?? 0) > (this.message(p.cle)?.id ?? 0));
        if (enAttente) this.adopter(a.cle, enAttente);
        else this.majMessage(a.cle, (m) => ({ ...m, enCours: false, attente: "La réponse arrive dès qu’elle est prête." }));
      }
      this.assistantsEnAttente.clear();
      this.maj({ echangeEnCours: false });
    }
    return this.message(a.cle) ?? null;
  }

  private adopter(cle: string, m: MessageFil): void {
    this.majMessage(cle, (x) => ({ ...m, cle: x.cle, etapes: m.etapes.length ? m.etapes : x.etapes, enCours: false, attente: undefined }));
  }

  private surEchange(ev: EvenementSSE, cleP: string, cleA: string): void {
    const d = (ev.donnees || {}) as Record<string, unknown>;
    switch (ev.type) {
      case "attente":
        this.majMessage(cleA, (m) => ({ ...m, attente: String(d.message || "") }));
        break;
      case "debut": {
        const conv = typeof d.conversation_id === "number" ? d.conversation_id : null;
        if (typeof d.message_id === "number") {
          const id = d.message_id;
          // Le flux a pu livrer la ligne avant : on retire le doublon.
          this.maj({ messages: this.etat.messages.filter((m) => m.id !== id || m.cle === cleP) });
          this.majMessage(cleP, (m) => ({ ...m, id }));
        }
        this.majMessage(cleA, (m) => ({ ...m, attente: undefined }));
        if (conv !== null && conv !== this.etat.conversationId) {
          // Conversation renouvelée par le serveur (12 h d'inactivité) : on la suit.
          this.maj({ conversationId: conv });
          void this.suivreFlux(conv);
          void this.listerConversations();
        }
        break;
      }
      case "masquage":
        this.majMessage(cleP, (m) => ({ ...m, masquage: { texte: String(d.texte ?? ""), types: (d.types as string[]) ?? [] } }));
        break;
      case "carte": {
        const carte = d as unknown as Carte;
        if (!carte || !carte.type) break;
        this.majMessage(cleA, (m) => (m.cartes.some((c) => c.id === carte.id) ? m : { ...m, cartes: [...m.cartes, carte] }));
        break;
      }
      case "etape": {
        const etape = d as unknown as Etape;
        this.majMessage(cleA, (m) => {
          const etapes = m.etapes.map((e, i) => (i === m.etapes.length - 1 && !e.fin ? { ...e, fin: etape.debut ?? Date.now() } : e));
          return { ...m, etapes: [...etapes, etape] };
        });
        break;
      }
      case "texte":
        this.majMessage(cleA, (m) => ({ ...m, texte: m.texte + String(d.delta ?? "") }));
        break;
      case "confirmation": {
        const c = d as unknown as Confirmation;
        this.majMessage(cleA, (m) => (m.confirmations.some((x) => x.id === c.id) ? m : { ...m, confirmations: [...m.confirmations, c] }));
        if (c.auto) this.maj({ decisions: { ...this.etat.decisions, [c.id]: "valider" } });
        break;
      }
      case "fin": {
        const id = typeof d.message_id === "number" ? d.message_id : undefined;
        if (id !== undefined) this.maj({ messages: this.etat.messages.filter((m) => m.id !== id || m.cle === cleA) });
        this.majMessage(cleA, (m) => {
          const confs = (d.confirmations as Confirmation[] | undefined) ?? [];
          const fusion = [...m.confirmations];
          for (const c of confs) if (!fusion.some((x) => x.id === c.id)) fusion.push(c);
          return {
            ...m,
            id,
            texte: String(d.texte ?? m.texte),
            etapes: Array.isArray(d.etapes) && (d.etapes as Etape[]).length ? (d.etapes as Etape[]) : m.etapes,
            confirmations: fusion,
            dureeMs: typeof d.duree_ms === "number" ? d.duree_ms : Date.now() - (m.debutMs ?? Date.now()),
            enCours: false,
            attente: undefined,
          };
        });
        // Les écrans alimentés par les points d'accès directs se relisent (l'outil a pu modifier la liste).
        const sujets = new Set((this.message(cleA)?.cartes ?? []).map((c) => sujetDeCarte(c.type)).filter((x): x is Sujet => !!x));
        sujets.forEach((x) => signalerMaj(x));
        break;
      }
      case "erreur":
        this.majMessage(cleA, (m) => ({ ...m, enCours: false, attente: undefined, erreur: String(d.message || "Erreur inattendue.") }));
        break;
    }
  }

  // --- flux long-vivant ---------------------------------------------------------------------------------------------

  private async suivreFlux(convId: number): Promise<void> {
    this.fluxControleur?.abort();
    const controleur = new AbortController();
    this.fluxControleur = controleur;
    let attente = 1000;
    while (this.actif && !controleur.signal.aborted && this.etat.conversationId === convId) {
      try {
        const r = await requete("GET", `/api/conversations/${convId}/flux?apres=${this.dernierId()}`, undefined, controleur.signal);
        if (r.status === 401) return;
        if (!r.ok) throw new ErreurApi(`flux ${r.status}`, r.status); // 429 (trop d'onglets) compris : nouvel essai plus tard
        if (this.etat.horsLigne) this.maj({ horsLigne: false });
        attente = 1000;
        await lireSSE(r, (ev) => this.surFlux(ev), controleur.signal);
      } catch (e) {
        if (controleur.signal.aborted) return;
        // Serveur injoignable (réseau coupé, Tailscale déconnecté) : l'historique reste lisible.
        this.maj({ horsLigne: e instanceof TypeError || !navigator.onLine });
        await new Promise((ok) => setTimeout(ok, attente));
        attente = Math.min(attente * 2, 30_000);
      }
    }
  }

  private surFlux(ev: EvenementSSE): void {
    const d = (ev.donnees || {}) as Record<string, unknown>;
    if (ev.type === "deconnecte") {
      // Session révoquée (autre appareil, Réglages) : retour à l'écran de connexion.
      this.fluxControleur?.abort();
      signalerDeconnexion();
      return;
    }
    if (ev.type === "maj") {
      // Données d'un écran modifiées ailleurs (autre adulte, Telegram, routine) : l'écran concerné se relit.
      const sujet = String(d.domaine || d.sujet || "*");
      signalerMaj((["courses", "echeances", "journal", "fichiers", "memoire", "sensibles"].includes(sujet) ? sujet : "*") as Sujet | "*");
      return;
    }
    if (ev.type === "message") this.recevoirMessage(depuisApi(d as unknown as MessageApi));
    else if (ev.type === "resultat_confirmation") {
      const id = Number(d.id);
      this.noterDecision(id, d.decision === "annuler" ? "annuler" : "valider", String(d.texte ?? ""), (d.cartes as Carte[]) ?? []);
    } else if (ev.type === "carte" && !this.etat.echangeEnCours) {
      // Carte hors échange (validation automatique, routine) : rangée dans un message de l'assistant.
      const carte = d as unknown as Carte;
      if (!carte?.type || this.etat.messages.some((m) => m.cartes.some((c) => c.id === carte.id))) return;
      this.maj({
        messages: [...this.etat.messages, { cle: nouvelleCle("s"), role: "assistant", texte: "", ts: new Date().toISOString(), cartes: [carte], etapes: [], confirmations: [] }],
      });
    }
    // Les autres événements (texte, étapes… d'un échange) arrivent par la réponse du POST ou par la ligne « message ».
  }

  private recevoirMessage(m: MessageFil): void {
    const etat = this.etat;
    if (m.id === undefined || etat.messages.some((x) => x.id === m.id)) return;
    const echange = etat.dernierEchange;
    if (etat.echangeEnCours && echange) {
      const p = this.message(echange.personne);
      if (m.role === "personne" && p && p.id === undefined) {
        this.majMessage(p.cle, (x) => ({ ...x, id: m.id }));
        return;
      }
      if (m.role !== "personne" && (m.id ?? 0) > (p?.id ?? 0)) {
        // Réponse de l'échange en cours livrée avant l'événement « fin » : gardée pour le cas où le POST se couperait.
        this.assistantsEnAttente.set(m.id!, m);
        return;
      }
    }
    if (m.decisionDe !== undefined) {
      const local = etat.messages.find((x) => x.cle === `conf-${m.decisionDe}`);
      if (local) {
        this.adopter(local.cle, m);
        return;
      }
    }
    // Une carte reçue seule (flux) puis enregistrée dans un message : la version seule disparaît.
    const ids = new Set(m.cartes.map((c) => c.id));
    const restants = ids.size
      ? etat.messages.filter((x) => !(x.cle.startsWith("s-") && x.cartes.every((c) => ids.has(c.id))))
      : etat.messages;
    const ailleurs = typeof location !== "undefined" && location.pathname !== "/" && m.role !== "personne" && m.spontane;
    this.maj({ messages: [...restants, m], ...(ailleurs ? { nonLus: etat.nonLus + 1 } : {}) });
  }

  marquerLus(): void {
    if (this.etat.nonLus) this.maj({ nonLus: 0 });
  }

  // --- confirmations ------------------------------------------------------------------------------------------------

  private noterDecision(id: number, decision: Decision, texte: string, cartes: Carte[]): void {
    this.maj({ decisions: { ...this.etat.decisions, [id]: decision } });
    if (!texte && !cartes.length) return;
    if (this.etat.messages.some((m) => m.decisionDe === id)) return;
    this.maj({
      messages: [
        ...this.etat.messages,
        { cle: `conf-${id}`, role: "systeme", texte, ts: new Date().toISOString(), cartes, etapes: [], confirmations: [], decisionDe: id },
      ],
    });
  }

  /** Action à valider née hors conversation (réglage d'une routine) : affichée comme une proposition de l'assistant. */
  ajouterConfirmationLocale(c: Confirmation, texte = ""): void {
    this.maj({
      messages: [
        ...this.etat.messages,
        { cle: nouvelleCle("l"), role: "assistant", texte, ts: new Date().toISOString(), cartes: [], etapes: [], confirmations: [c] },
      ],
    });
  }

  async decider(id: number, decision: Decision): Promise<void> {
    if (this.etat.decisions[id] === "en_cours") return;
    const avant = this.etat.decisions[id];
    this.maj({ decisions: { ...this.etat.decisions, [id]: "en_cours" } });
    try {
      const r = await api<{ texte: string; cartes: Carte[] }>("POST", `/api/confirmations/${id}`, { decision });
      this.noterDecision(id, decision, r.texte, r.cartes ?? []);
    } catch (e) {
      const d = { ...this.etat.decisions };
      if (avant) d[id] = avant;
      else delete d[id];
      this.maj({ decisions: d });
      toast(messageErreur(e));
    }
  }
}

export const magasin = new MagasinConversation();

export function useConversation<T>(selection: (e: EtatConversation) => T): T {
  return useSyncExternalStore(magasin.abonner, () => selection(magasin.etat));
}

/** Dernière carte d'un des types donnés dans le fil (la plus récente d'abord), avec son message. */
export function derniereCarte(messages: MessageFil[], types: string[]): Carte | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const cartes = messages[i].cartes;
    for (let j = cartes.length - 1; j >= 0; j--) if (types.includes(cartes[j].type)) return cartes[j];
  }
  return null;
}

export function cartesDuType(messages: MessageFil[], types: string[], limite = 20): Carte[] {
  const sortie: Carte[] = [];
  for (let i = messages.length - 1; i >= 0 && sortie.length < limite; i--)
    for (const c of [...messages[i].cartes].reverse()) if (types.includes(c.type) && sortie.length < limite) sortie.push(c);
  return sortie;
}

export const aujourdhuiIso = () => isoJour(new Date());

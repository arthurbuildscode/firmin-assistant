import { describe, expect, it, vi, beforeEach } from "vitest";
import { MagasinConversation } from "./conversation";

function reponseSSE(blocs: [string, unknown][]): Response {
  const texte = blocs.map(([t, d]) => `event: ${t}\ndata: ${JSON.stringify(d)}\n\n`).join("");
  const flux = new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(texte));
      c.close();
    },
  });
  return new Response(flux, { status: 200, headers: { "content-type": "text/event-stream" } });
}

describe("échange en SSE", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("assemble masquage, étapes, texte, carte, confirmation et fin dans le fil", async () => {
    const m = new MagasinConversation();
    m.etat = { ...m.etat, conversationId: 5, charge: true };
    vi.stubGlobal("fetch", vi.fn(async () =>
      reponseSSE([
        ["debut", { conversation_id: 5, message_id: 10 }],
        ["masquage", { texte: "le code est [mot de passe masqué]", types: ["mot_de_passe"], message_id: 10 }],
        ["etape", { etape: "Je lis l'agenda…", outil: "agenda_lire", debut: 1000 }],
        ["carte", { type: "agenda_jour", id: "c-1", donnees: { date: "2026-11-07", evenements: [] }, actions: [] }],
        ["texte", { delta: "Samedi " }],
        ["texte", { delta: "CARTE:{}\npiscine." }],
        ["confirmation", { id: 3, resume: "Ajouter", auto: false, step_up: false }],
        ["fin", { texte: "Samedi piscine.", message_id: 11, duree_ms: 900, etapes: [{ etape: "Je lis l'agenda…", duree_ms: 400 }], confirmations: [{ id: 3, resume: "Ajouter" }] }],
      ]),
    ));
    const reponse = await m.envoyer("le code est 1234", { contexte: { ecran: "conversation" } });
    const [p, a] = m.etat.messages;
    expect(p).toMatchObject({ role: "personne", id: 10, masquage: { texte: "le code est [mot de passe masqué]" } });
    expect(a).toMatchObject({ role: "assistant", id: 11, texte: "Samedi piscine.", enCours: false, dureeMs: 900 });
    expect(a.cartes.map((c) => c.id)).toEqual(["c-1"]);
    expect(a.confirmations).toHaveLength(1);
    expect(reponse?.id).toBe(11);
    expect(m.etat.echangeEnCours).toBe(false);
  });

  it("une erreur du serveur s'affiche sans faire planter le fil", async () => {
    const m = new MagasinConversation();
    m.etat = { ...m.etat, conversationId: 5, charge: true };
    vi.stubGlobal("fetch", vi.fn(async () => reponseSSE([["debut", { conversation_id: 5, message_id: 1 }], ["erreur", { message: "Claude indisponible." }]])));
    await m.envoyer("bonjour");
    expect(m.etat.messages[1]).toMatchObject({ enCours: false, erreur: "Claude indisponible." });
  });
});

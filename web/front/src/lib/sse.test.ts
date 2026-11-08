import { describe, expect, it } from "vitest";
import { AnalyseurSSE, analyserBloc, lireSSE } from "./sse";

describe("analyse SSE", () => {
  it("rend les événements typés avec leurs données JSON", () => {
    const a = new AnalyseurSSE();
    const evs = a.pousser('event: debut\ndata: {"conversation_id": 3, "message_id": 7}\n\nevent: texte\ndata: {"delta": "Bon"}\n\n');
    expect(evs).toEqual([
      { type: "debut", donnees: { conversation_id: 3, message_id: 7 }, id: undefined },
      { type: "texte", donnees: { delta: "Bon" }, id: undefined },
    ]);
  });

  it("recolle les événements coupés entre deux morceaux, CRLF compris", () => {
    const a = new AnalyseurSSE();
    expect(a.pousser("event: carte\r\nda")).toEqual([]);
    expect(a.pousser('ta: {"type": "courses", "id": "c-1"}\r')).toEqual([]);
    const evs = a.pousser("\n\r\n");
    expect(evs).toHaveLength(1);
    expect(evs[0]).toMatchObject({ type: "carte", donnees: { type: "courses", id: "c-1" } });
  });

  it("ignore les commentaires (ping) et les blocs sans données", () => {
    const a = new AnalyseurSSE();
    expect(a.pousser(": ping - 2026-11-07 10:00:00\n\nevent: rien\n\n")).toEqual([]);
  });

  it("joint les lignes data multiples et garde le texte non JSON", () => {
    expect(analyserBloc("data: ligne 1\ndata: ligne 2")).toEqual({ type: "message", donnees: "ligne 1\nligne 2", id: undefined });
    expect(analyserBloc("id: 12\nevent:fin\ndata:{\"texte\":\"ok\"}")).toEqual({ type: "fin", donnees: { texte: "ok" }, id: "12" });
  });

  it("lit une réponse fetch en flux et rend le dernier événement sans ligne vide finale", async () => {
    const encodeur = new TextEncoder();
    const morceaux = ["event: etape\ndata: {\"etape\": \"Je lis l’agenda…\"}\n", "\nevent: fin\ndata: {\"texte\": \"é\"}"];
    const flux = new ReadableStream({
      start(c) {
        for (const m of morceaux) c.enqueue(encodeur.encode(m));
        c.close();
      },
    });
    const recus: string[] = [];
    await lireSSE(new Response(flux), (ev) => recus.push(ev.type));
    expect(recus).toEqual(["etape", "fin"]);
  });
});

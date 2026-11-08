// Fidélité à la maquette et retours d'usage : dock (prérempli, voix/envoyer, réponse d'écran sur une
// ligne), écran Courses (point d'accès direct ou repli sur la carte du fil, cocher sur place), barre d'outils sous une
// réponse, cartes du jour avant la question, permission proposée en contexte une seule fois.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Dock } from "./Dock";
import { Message } from "./Message";
import { PropositionsContexte } from "./Propositions";
import { FournisseurInfoBulles } from "./ui/tooltip";
import { EcranCourses } from "@/ecrans/Courses";
import { EcranConversation } from "@/ecrans/Conversation";
import { preremplirDock } from "@/lib/dock";
import { libelleRayon, rayonsDe } from "@/lib/donnees";
import { definirSession } from "@/lib/session";
import type { Carte, MessageFil } from "@/lib/types";
import { magasin } from "@/store/conversation";

function json(corps: unknown, status = 200): Response {
  return new Response(JSON.stringify(corps), { status, headers: { "content-type": "application/json" } });
}

const notifier = () => (magasin as unknown as { ecouteurs: Set<() => void> }).ecouteurs.forEach((f) => f());
function etat(p: Partial<typeof magasin.etat>) {
  magasin.etat = { ...magasin.etat, ...p };
  notifier();
}

const msg = (p: Partial<MessageFil>): MessageFil => ({ cle: `m${Math.random()}`, role: "assistant", texte: "", ts: "2026-11-08T08:00:00", cartes: [], etapes: [], confirmations: [], ...p });
const carteCourses: Carte = {
  type: "courses",
  id: "c-courses",
  donnees: { restants: 2, rayons: [{ rayon: "laitier", articles: [{ id: 1, article: "Lait", coche: false, ajoute_par: "Alex" }, { id: 2, article: "Beurre", coche: false }] }] },
  actions: [],
};

beforeEach(() => {
  definirSession({ connecte: true, personne: { prenom: "Alex", linux_user: "alex", role: "proprietaire" }, assistant: "Firmin" });
  magasin.etat = { ...magasin.etat, decisions: {}, messages: [], charge: true, echangeEnCours: false, dernierEcran: null, dernierEchange: null, accueil: [], accueilDate: "2026-11-08" };
  try {
    localStorage.clear();
  } catch {
    /* rien */
  }
});
afterEach(() => vi.restoreAllMocks());

describe("dock", () => {
  it("écran dédié : « Demander à Firmin… », bouton voix champ vide, Envoyer dès qu'il y a du texte", () => {
    render(<Dock ecran="courses" />, { wrapper: FournisseurInfoBulles });
    const champ = screen.getByPlaceholderText("Demander à Firmin…") as HTMLInputElement;
    expect(champ.value).toBe("");
    expect(screen.getByRole("button", { name: /Mode voix/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Envoyer" })).toBeNull();
    fireEvent.change(champ, { target: { value: "des œufs" } });
    expect(screen.getByRole("button", { name: "Envoyer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Mode voix/ })).toBeNull();
  });

  it("un texte prérempli non complété disparaît quand on change d'écran ; un texte modifié reste", () => {
    const { rerender } = render(<Dock ecran="semaine" />, { wrapper: FournisseurInfoBulles });
    act(() => preremplirDock("Ajoute à l’agenda : "));
    const champ = () => screen.getByPlaceholderText("Demander à Firmin…") as HTMLInputElement;
    expect(champ().value).toBe("Ajoute à l’agenda : ");
    rerender(<Dock ecran="courses" />);
    expect(champ().value).toBe("");
    act(() => preremplirDock("Note au journal : "));
    fireEvent.change(champ(), { target: { value: "Note au journal : première dent de Nina" } });
    rerender(<Dock ecran="semaine" />);
    expect(champ().value).toBe("Note au journal : première dent de Nina");
  });

  it("réponse à une demande faite depuis l'écran : une ligne dans le dock, sans bulle, méta ni réflexion", () => {
    const p = msg({ cle: "p1", role: "personne", texte: "Montre-moi la liste", ecran: "courses" });
    const a = msg({ cle: "a1", texte: "Voici la **liste** : 2 articles.", cartes: [carteCourses], etapes: [{ etape: "Je lis la liste…" }] });
    etat({ messages: [p, a], dernierEcran: "courses", dernierEchange: { personne: "p1", assistant: "a1" } });
    const { container } = render(<Dock ecran="courses" />, { wrapper: FournisseurInfoBulles });
    expect(container.querySelector("[data-reponse-ecran]")?.textContent).toContain("Voici la liste : 2 articles.");
    expect(container.querySelector(".bulle, .reflexion, .meta-message")).toBeNull();
    expect(screen.queryByText("À propos de cette liste")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Masquer la réponse" }));
    expect(screen.getByText("À propos de cette liste")).toBeTruthy();
  });
});

describe("écran Courses", () => {
  it("repli sans point d'accès : la dernière carte du fil, en plein écran, rayons lisibles, sans la carte du jour", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ detail: "Not Found" }, 404));
    const bonjour: Carte = { type: "bonjour", id: "c-b", donnees: { date: "2026-11-08", prenom: "Alex" }, actions: [] };
    etat({ messages: [msg({ cartes: [bonjour, carteCourses], texte: "Il n’y a que du lait." })] });
    const { container } = render(<EcranCourses />);
    await waitFor(() => expect(screen.getByText("Crèmerie")).toBeTruthy());
    expect(screen.getByText("Lait")).toBeTruthy();
    expect(container.querySelector("[data-carte]")).toBeNull();
    expect(container.textContent).not.toContain("Bonjour");
    fireEvent.click(screen.getByText("Lait"));
    expect(screen.getByText("1 changement à enregistrer")).toBeTruthy();
  });

  it("point d'accès direct : cocher sur place (PATCH), sans réordonner ni passer par la conversation", async () => {
    const liste = { rayons: [{ rayon: "Crèmerie", articles: [{ id: 1, article: "Lait", coche: false }, { id: 2, article: "Beurre", coche: false }] }] };
    const appels: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (chemin, init) => {
      appels.push(`${init?.method} ${chemin}`);
      // Contrat de web/ecrans.py : PATCH renvoie l'article à jour, la liste ne se réordonne pas.
      if (init?.method === "PATCH") return json({ article: { id: 1, article: "Lait", rayon: "laitier", coche: true } });
      return json(liste);
    });
    const envoyer = vi.spyOn(magasin, "envoyer");
    render(<EcranCourses />);
    await waitFor(() => expect(screen.getByText("Beurre")).toBeTruthy());
    const ordre = () => Array.from(document.querySelectorAll(".article .nm")).map((x) => x.textContent);
    const avant = ordre();
    fireEvent.click(screen.getByText("Lait"));
    await waitFor(() => expect(appels).toContain("PATCH /api/courses/1"));
    expect(ordre()).toEqual(avant);
    expect(document.querySelector(".article.coche")?.textContent).toContain("Lait");
    expect(screen.queryByText(/changement à enregistrer/)).toBeNull();
    expect(envoyer).not.toHaveBeenCalled();
  });

  it("normalise les deux formes de liste et les rayons des outils", () => {
    expect(rayonsDe({ articles: [{ article: "Lait", rayon: "laitier" }, { article: "Pain", rayon: "boulangerie" }] })?.map((r) => r.rayon)).toEqual(["laitier", "boulangerie"]);
    expect(rayonsDe({ autre: 1 })).toBeNull();
    expect(libelleRayon("laitier")).toBe("Crèmerie");
    expect(libelleRayon("fruits-légumes")).toBe("Fruits et légumes");
    expect(libelleRayon("Animaux")).toBe("Animaux");
  });
});

describe("fil de conversation", () => {
  it("barre d'outils sous une réponse : icônes seules ; pouce bas → commentaire facultatif enregistré", async () => {
    const appels: { chemin: string; corps: unknown }[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (chemin, init) => {
      appels.push({ chemin: String(chemin), corps: init?.body ? JSON.parse(String(init.body)) : null });
      return json({ ok: true, id: 1 });
    });
    const question = msg({ cle: "q", role: "personne", texte: "Qu’est-ce qu’on mange ?" });
    render(<Message m={msg({ id: 12, texte: "Ce soir, dahl." })} question={question} />, { wrapper: FournisseurInfoBulles });
    const barre = screen.getByRole("toolbar", { name: "Actions sur la réponse" });
    expect(barre.textContent).toBe(""); // aucun libellé texte
    for (const nom of ["Copier la réponse", "Écouter", "Réponse utile", "Réponse pas utile", "Réessayer : renvoyer la même demande"])
      expect(screen.getByRole("button", { name: nom })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Réponse pas utile" }));
    const champ = screen.getByLabelText("Qu’est-ce qui n’allait pas ? (facultatif)");
    fireEvent.change(champ, { target: { value: "Trop court" } });
    fireEvent.submit(champ.closest("form")!);
    await waitFor(() => expect(appels.filter((a) => a.chemin === "/api/messages/12/avis")).toHaveLength(2));
    expect(appels.map((a) => a.corps)).toEqual([{ note: -1 }, { note: -1, commentaire: "Trop court" }]);
    const envoyer = vi.spyOn(magasin, "envoyer").mockResolvedValue(null);
    fireEvent.click(screen.getByRole("button", { name: "Réessayer : renvoyer la même demande" }));
    expect(envoyer).toHaveBeenCalledWith("Qu’est-ce qu’on mange ?", { contexte: { ecran: "conversation" } });
  });

  it("cartes du jour posées avant la question qui les a fait naître, dans l'ordre bonjour, À noter, brief", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json({ date: "2026-11-08", cartes: [] }));
    Element.prototype.scrollIntoView = () => {};
    const jour: Carte[] = [
      { type: "brief", id: "c-brief", titre: "Brief du jour", donnees: { texte: "Journée calme." }, actions: [] },
      { type: "a_noter", id: "c-note", donnees: { elements: [] }, actions: [] },
      { type: "bonjour", id: "c-bj", titre: "Bonjour Alex", donnees: { date: "2026-11-08", prenom: "Alex" }, actions: [] },
    ];
    etat({ messages: [msg({ cle: "p", role: "personne", texte: "Montre-moi la liste" }), msg({ cle: "a", id: 3, texte: "Voici.", cartes: [...jour, carteCourses] })] });
    const { container } = render(<EcranConversation />, { wrapper: FournisseurInfoBulles });
    const ordre = Array.from(container.querySelectorAll("[data-carte], .bulle.moi")).map((x) => x.getAttribute("data-carte") ?? "question");
    expect(ordre).toEqual(["bonjour", "a_noter", "brief", "question", "courses"]);
  });

  it("message spontané (brief du matin) : la carte seule, sans bulle doublon ni « Réfléchi », méta discrète", () => {
    const texte = "Journée calme : réunion à 9 h.";
    const brief: Carte = { type: "brief", id: "c-br", titre: "Brief du jour", donnees: { texte, periode: "jour" }, actions: [] };
    const { container } = render(<Message m={msg({ id: 40, texte, canal: "routine", spontane: true, cartes: [brief] })} />, { wrapper: FournisseurInfoBulles });
    expect(container.querySelector("[data-carte='brief']")).toBeTruthy();
    expect(container.querySelector(".bulle")).toBeNull();
    expect(container.querySelector(".reflexion")).toBeNull();
    expect(container.querySelectorAll(".meta-message").length).toBeLessThanOrEqual(1);
  });

  it("« Tu veux que je te prévienne ici ? » après le premier échange réussi, une seule fois", () => {
    const demander = vi.fn(async () => "granted" as NotificationPermission);
    vi.stubGlobal("Notification", Object.assign(function () {}, { permission: "default", requestPermission: demander }));
    vi.stubGlobal("PushManager", function () {});
    Object.defineProperty(navigator, "serviceWorker", { value: { ready: new Promise(() => {}), getRegistration: async () => undefined }, configurable: true });
    const { rerender } = render(<PropositionsContexte messages={[]} />);
    expect(screen.queryByText(/prévienne ici/)).toBeNull();
    const fil = [msg({ role: "personne", texte: "Salut" }), msg({ id: 2, texte: "Bonjour !" })];
    rerender(<PropositionsContexte messages={fil} />);
    expect(screen.getByText("Tu veux que je te prévienne ici (brief, validations) ?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Plus tard" }));
    expect(screen.queryByText(/prévienne ici/)).toBeNull();
    rerender(<PropositionsContexte messages={[...fil]} />);
    expect(screen.queryByText(/prévienne ici/)).toBeNull();
    vi.unstubAllGlobals();
  });
});

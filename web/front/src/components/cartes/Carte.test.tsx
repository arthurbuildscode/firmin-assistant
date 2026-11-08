import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Carte } from "./Carte";
import { magasin } from "@/store/conversation";
import { definirSession } from "@/lib/session";
import type { Carte as TCarte } from "@/lib/types";

const carte = (type: string, donnees: Record<string, unknown>, extra: Partial<TCarte> = {}): TCarte => ({ type, id: `c-${type}`, donnees, actions: [], ...extra });

beforeEach(() => {
  definirSession({ connecte: true, personne: { prenom: "Alice", linux_user: "alice", role: "proprietaire" }, assistant: "Firmin" });
  magasin.etat = { ...magasin.etat, decisions: {}, messages: [] };
  vi.restoreAllMocks();
});

describe("cartes génératives", () => {
  it("transports : l'alerte d'abord, puis les départs en minutes ; suppression barrée ; théorique signalé", () => {
    render(
      <Carte
        carte={carte("transports_prochains", {
          trajets: [{
            titre: "Car 7 · Mairie → Gare", mode: "car", alerte: "retard 7 min sur le départ de 16:25",
            avertissement: null,
            departs: [
              { heure_theorique: "16:25", heure_estimee: "16:32", retard_min: 7, dans_min: 22, statut: "a_venir" },
              { heure_theorique: "16:55", heure_estimee: null, retard_min: null, dans_min: 45, statut: "supprime" },
              { heure_theorique: "17:25", heure_estimee: null, retard_min: null, dans_min: 75, statut: "prevu" },
            ],
          }],
        }, { titre: "Car 7 · Mairie → Gare" })}
      />,
    );
    expect(screen.getByText("Retard 7 min sur le départ de 16:25")).toBeTruthy();
    expect(screen.getByText("dans 22 min")).toBeTruthy();
    expect(screen.getByText("16:32 · +7 min")).toBeTruthy();
    expect(screen.getByText("supprimé").closest(".ligne-carte")?.className).toMatch(/line-through/);
    expect(screen.getByText("17:25 · théorique")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/transports_prochains|fournisseur|gtfs/i);
  });

  it("état d'un train : statut, départ et arrivée, arrêts au geste", () => {
    render(
      <Carte
        carte={carte("transports_etat", {
          numero: "6105", mode: "train", statut: "en_retard", retard_min: 12,
          depart: { lieu: "Marseille", prevu: "08:12", estime: "08:24" }, arrivee: { lieu: "Paris", prevu: "11:30", estime: "11:42" },
          arrets: [{ lieu: "Marseille", prevu: "08:12" }, { lieu: "Avignon", prevu: "08:45" }, { lieu: "Paris", prevu: "11:30" }],
          perturbations: [{ titre: "Ralentissement" }], message: null,
        }, { titre: "Train 6105" })}
      />,
    );
    expect(screen.getByText(/En retard · \+12 min/)).toBeTruthy();
    expect(screen.getByText("08:24 (prévu 08:12)")).toBeTruthy();
    expect(screen.getByText("3 arrêts")).toBeTruthy();
    expect(screen.getByText("Ralentissement")).toBeTruthy();
  });

  it("agenda d'un jour : heures et titres, sans nom technique", () => {
    render(<Carte carte={carte("agenda_jour", { date: "2026-11-07", evenements: [{ titre: "Piscine", debut: "2026-11-07T10:00", agenda: "famille", lieu: "Centre aquatique" }] })} />);
    expect(screen.getByText("Piscine")).toBeTruthy();
    expect(screen.getByText("10:00")).toBeTruthy();
    expect(screen.getByText(/Centre aquatique/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/agenda_jour|mcp__/);
  });

  it("courses compactes : trois rayons, articles cochés absents, nouveaux en gras", () => {
    render(
      <Carte
        carte={carte("courses", {
          restants: 3,
          ajoutes: ["Lait"],
          rayons: [
            { rayon: "Crèmerie", articles: [{ article: "Lait" }, { article: "Beurre", coche: true }] },
            { rayon: "Épicerie", articles: [{ article: "Café" }] },
            { rayon: "Boulangerie", articles: [{ article: "Pain" }] },
            { rayon: "Animaux", articles: [{ article: "Croquettes" }] },
          ],
        })}
      />,
    );
    expect(screen.getByText("Lait").tagName).toBe("B");
    expect(screen.queryByText("Beurre")).toBeNull();
    expect(screen.getByText(/1 article dans les autres rayons/)).toBeTruthy();
  });

  it("proposition à valider : Valider / Modifier / Annuler, puis l'état remplace les boutons sur place", async () => {
    const decider = vi.spyOn(magasin, "decider").mockImplementation(async (id, d) => {
      magasin.etat = { ...magasin.etat, decisions: { ...magasin.etat.decisions, [id]: d } };
      (magasin as unknown as { ecouteurs: Set<() => void> }).ecouteurs.forEach((f) => f());
    });
    render(
      <Carte
        carte={carte("evenement", { titre: "Dentiste", debut: "2026-11-13T17:00", statut: "a_valider", agenda: "famille" }, {
          actions: [
            { libelle: "Valider", action: "confirmer", args: { decision: "valider" }, confirmation: 42 },
            { libelle: "Modifier", action: "message", args: { texte: "modifier" } },
            { libelle: "Annuler", action: "confirmer", args: { decision: "annuler" }, confirmation: 42 },
          ],
        })}
      />,
    );
    expect(screen.getByRole("button", { name: "Modifier" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Valider" }));
    await waitFor(() => expect(screen.getByText("Validé")).toBeTruthy());
    expect(decider).toHaveBeenCalledWith(42, "valider");
    expect(screen.queryByRole("button", { name: "Annuler" })).toBeNull();
  });

  it("À noter : raison en clair, en retard signalé, le reste plus tard replié", () => {
    render(
      <Carte
        carte={carte("a_noter", {
          aujourdhui: 1,
          semaine: 1,
          elements: [
            { type: "echeance", titre: "Vermifuge", date: "2026-11-08", dans_jours: 1, notes: "à acheter, il n'en reste plus" },
            { type: "echeance", titre: "Assurance", date: "2026-11-01", dans_jours: -6, en_retard: true },
            ...[10, 11, 12, 13].map((j) => ({ type: "echeance", titre: `Plus tard ${j}`, date: "2026-11-20", dans_jours: j })),
          ],
        })}
      />,
    );
    expect(screen.getByText("à acheter, il n'en reste plus")).toBeTruthy();
    expect(screen.getByText("En retard")).toBeTruthy();
    expect(screen.queryByText("Plus tard 13")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /2 autres plus tard/ }));
    expect(screen.getByText("Plus tard 13")).toBeTruthy();
  });

  it("identifiants (lot 5c) : formulaire masqué, valeurs envoyées au coffre et jamais gardées", async () => {
    const appels: { url: string; corps: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      appels.push({ url, corps: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify({ ok: true, champs: ["identifiant", "mot_de_passe"] }), { status: 200 });
    }));
    render(
      <Carte
        carte={carte("identifiants", { cle: "libre/cantine", type: "identifiants", champs: ["identifiant", "mot_de_passe"], libelle: "Cantine" }, {
          titre: "Cantine",
          actions: [{ libelle: "Enregistrer", action: "coffre_ecrire", args: { cle: "libre/cantine" }, confirmation: "step-up" }],
        })}
      />,
    );
    const mdp = screen.getByLabelText("Mot de passe") as HTMLInputElement;
    expect(mdp.type).toBe("password");
    fireEvent.change(screen.getByLabelText("Identifiant"), { target: { value: "parent" } });
    fireEvent.change(mdp, { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: /Enregistrer/ }));
    await waitFor(() => expect(screen.getByText(/Enregistré dans le coffre/)).toBeTruthy());
    expect(appels).toEqual([{ url: "/api/coffre", corps: { cle: "libre/cantine", champs: { identifiant: "parent", mot_de_passe: "s3cret" } } }]);
    expect(document.body.innerHTML).not.toContain("s3cret");
    expect(JSON.stringify(magasin.etat)).not.toContain("s3cret");
    vi.unstubAllGlobals();
  });

  it("capture : seulement une image servie par l'appli", () => {
    const { container, rerender } = render(<Carte carte={carte("capture", { url: "https://pistage.example/pixel.png" })} />);
    expect(container.querySelector("img")).toBeNull();
    rerender(<Carte carte={carte("capture", { url: "/api/captures/selection.png", legende: "Sélection" })} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/api/captures/selection.png");
  });

  it("document sensible : verrouillé, rien du contenu n'est affiché", () => {
    render(<Carte carte={carte("document_sensible", { nom: "Carnet de santé", acces: "parents" })} />);
    expect(screen.getByText("Carnet de santé")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Déverrouiller Carnet/ })).toBeTruthy();
  });

  it("météo : commune du foyer, températures, pluie, moments de la journée, sans nom technique", () => {
    render(
      <Carte
        carte={carte(
          "meteo",
          {
            commune: "Villeneuve",
            date: "2026-11-08",
            libelle: "Averses",
            icone: "averse",
            min: 8.2,
            max: 17.4,
            pluie_pct: 70,
            pluie_mm: 3.4,
            vent_kmh: 22,
            actuel: { temperature: 14.2, libelle: "Partiellement nuageux", icone: "nuage-soleil" },
            moments: [
              { moment: "matin", temperature: 11, pluie_pct: 10, libelle: "Plutôt dégagé", icone: "soleil" },
              { moment: "après-midi", temperature: 16, pluie_pct: 70, libelle: "Averses", icone: "averse" },
              { moment: "soir", temperature: 12, pluie_pct: 10, libelle: "Plutôt dégagé", icone: "soleil" },
            ],
          },
          { titre: "Météo aujourd'hui à Villeneuve", actions: [{ libelle: "Carte du jour", action: "ouvrir", args: { ecran: "jour", date: "2026-11-08" } }] },
        )}
      />,
    );
    expect(screen.getByText("Météo aujourd'hui à Villeneuve")).toBeTruthy();
    expect(screen.getByText("17°")).toBeTruthy();
    expect(screen.getByText(/8° au plus bas · pluie 70 % \(3.4 mm\) · vent 22 km\/h/)).toBeTruthy();
    expect(screen.getByText(/En ce moment/).textContent).toContain("14°, partiellement nuageux");
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("après-midi")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Carte du jour" })).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/meteo_lire|nuage-soleil|averse\b/);
  });

  it("type sans rendu dédié : titre et texte seulement", () => {
    render(<Carte carte={carte("type_futur", { texte: "Bonjour" }, { titre: "Nouveauté" })} />);
    expect(screen.getByText("Nouveauté")).toBeTruthy();
    expect(screen.getByText("Bonjour")).toBeTruthy();
  });
});

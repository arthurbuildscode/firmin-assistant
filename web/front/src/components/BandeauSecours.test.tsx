import { describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { BandeauSecours } from "./BandeauSecours";
import { definirSession } from "@/lib/session";
import { detailSession } from "@/ecrans/Reglages";

const personne = { prenom: "Alex", linux_user: "alex", role: "proprietaire" };

describe("connexion de secours", () => {
  it("affiche le bandeau seulement pour une session ouverte par code", () => {
    act(() => definirSession({ connecte: true, personne, niveau: "passkey" }));
    const { rerender } = render(<BandeauSecours />);
    expect(screen.queryByText(/Connexion de secours/)).toBeNull();
    act(() => definirSession({ connecte: true, personne, niveau: "secours" }));
    rerender(<BandeauSecours />);
    expect(screen.getByRole("button").textContent).toContain("Connexion de secours · ajoute une passkey");
  });

  it("Réglages : compte Tailscale et niveau de chaque session", () => {
    const base = { id: 1, appareil: "Safari — iPhone", cree: "", vu: "", methode: "code", courante: true };
    expect(detailSession({ ...base, niveau: "secours", tailscale_login: "alex@exemple.org" }, "auj. · 08:12")).toBe(
      "Vu auj. · 08:12 · vu depuis alex@exemple.org · connexion de secours (24 h)",
    );
    expect(detailSession({ ...base, methode: "passkey", niveau: "passkey" }, "hier")).toBe("Vu hier");
  });
});

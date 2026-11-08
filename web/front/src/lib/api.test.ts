// Step-up à usage unique du coffre (CONTRACT §12) : chaque écriture consomme le jeton ; le front redemande
// l'empreinte à chaque saisie, y compris pour deux saisies d'affilée. Session de secours : pas de step-up proposé.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ErreurApi, oublierStepUp } from "./api";

const tampon = () => new Uint8Array([1, 2, 3]).buffer;
const fausseAssertion = {
  id: "AQID",
  rawId: tampon(),
  type: "public-key",
  getClientExtensionResults: () => ({}),
  response: { clientDataJSON: tampon(), authenticatorData: tampon(), signature: tampon(), userHandle: null },
};

function json(corps: unknown, status = 200): Response {
  return new Response(JSON.stringify(corps), { status, headers: { "content-type": "application/json" } });
}

/** Faux serveur : un jeton de step-up valide à la fois, consommé par chaque écriture du coffre. */
function fauxServeur() {
  const etat = { jeton: null as string | null, emis: 0, ecritures: [] as (string | null)[] };
  const fetch = vi.fn(async (chemin: string, init: RequestInit) => {
    const entetes = (init.headers ?? {}) as Record<string, string>;
    if (chemin === "/auth/step-up/options") return json({ ceremonie: "c", options: { challenge: "AAAA" } });
    if (chemin === "/auth/step-up") {
      etat.jeton = `jeton-${++etat.emis}`;
      return json({ jeton: etat.jeton, expire: new Date(Date.now() + 5 * 60_000).toISOString() });
    }
    if (chemin === "/api/coffre") {
      const presente = entetes["X-Homeos-Step-Up"] ?? null;
      etat.ecritures.push(presente);
      if (!presente || presente !== etat.jeton) return json({ detail: "Confirme ton identité.", step_up: true }, 403);
      etat.jeton = null; // usage unique
      return json({ ok: true, champs: ["code"] });
    }
    return json({ detail: "inconnu" }, 404);
  });
  return { etat, fetch };
}

describe("step-up à usage unique (coffre)", () => {
  let get: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    oublierStepUp();
    get = vi.fn(async () => fausseAssertion);
    Object.defineProperty(navigator, "credentials", { value: { get }, configurable: true });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("redemande l'empreinte pour deux saisies d'affilée", async () => {
    const { etat, fetch } = fauxServeur();
    vi.stubGlobal("fetch", fetch);
    await expect(api("POST", "/api/coffre", { cle: "libre/a", champs: { code: "1" } })).resolves.toMatchObject({ ok: true });
    await expect(api("POST", "/api/coffre", { cle: "libre/b", champs: { code: "2" } })).resolves.toMatchObject({ ok: true });
    expect(get).toHaveBeenCalledTimes(2);
    // 1re saisie : sans jeton → 403, empreinte, jeton-1. 2e : jeton-1 déjà consommé → 403, empreinte, jeton-2.
    expect(etat.ecritures).toEqual([null, "jeton-1", "jeton-1", "jeton-2"]);
  });

  it("session de secours : message clair, pas de demande d'empreinte", async () => {
    const message = "Connecte-toi par empreinte pour ça : la connexion de secours (code Telegram) permet seulement…";
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: message, message, secours: true }, 403)));
    const erreur = (await api("POST", "/api/coffre", { cle: "libre/a", champs: { code: "1" } }).catch((e: unknown) => e)) as ErreurApi;
    expect(erreur).toBeInstanceOf(ErreurApi);
    expect(erreur.message).toBe(message);
    expect(erreur.stepUp).toBe(false);
    expect(get).not.toHaveBeenCalled();
  });
});

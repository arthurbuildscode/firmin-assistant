// La sortie PRF d'une passkey (clé des documents sensibles) ne quitte jamais le navigateur.
import { describe, expect, it } from "vitest";
import { credentialJson, extensionsSansPrf, sortiePrf } from "./webauthn";

const sortie = new Uint8Array(32).fill(7).buffer;

function fausseAssertion(): PublicKeyCredential {
  const vide = new Uint8Array([1, 2, 3]).buffer;
  return {
    id: "AQID",
    rawId: vide,
    type: "public-key",
    getClientExtensionResults: () => ({ prf: { enabled: true, results: { first: sortie } } }) as AuthenticationExtensionsClientOutputs,
    response: { clientDataJSON: vide, authenticatorData: vide, signature: vide, userHandle: null },
  } as unknown as PublicKeyCredential;
}

describe("passkeys et PRF", () => {
  it("la réponse envoyée au serveur ne contient pas la sortie PRF", () => {
    const json = credentialJson(fausseAssertion());
    expect(json.clientExtensionResults).toEqual({ prf: { enabled: true } });
    expect(JSON.stringify(json)).not.toContain("results");
    expect(extensionsSansPrf({ credProps: { rk: true } } as AuthenticationExtensionsClientOutputs)).toEqual({ credProps: { rk: true } });
  });

  it("la sortie PRF est lue (copie de 32 octets) pour dériver la clé dans le navigateur", () => {
    const o = sortiePrf(fausseAssertion());
    expect(o).toHaveLength(32);
    expect(o![0]).toBe(7);
  });
});

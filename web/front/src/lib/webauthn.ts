// Passkeys (WebAuthn) : conversions base64url <-> ArrayBuffer et cérémonies (même protocole que web/static/index.html).

/* eslint-disable @typescript-eslint/no-explicit-any */
export const versOctets = (b64: string): ArrayBuffer => {
  const normal = b64.replace(/-/g, "+").replace(/_/g, "/");
  const complet = normal.padEnd(Math.ceil(normal.length / 4) * 4, "=");
  return Uint8Array.from(atob(complet), (c) => c.charCodeAt(0)).buffer;
};

export const versB64 = (tampon: ArrayBuffer): string => {
  let binaire = "";
  for (const o of new Uint8Array(tampon)) binaire += String.fromCharCode(o);
  return btoa(binaire).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

export function optionsCreation(o: any): PublicKeyCredentialCreationOptions {
  return {
    ...o,
    challenge: versOctets(o.challenge),
    user: { ...o.user, id: versOctets(o.user.id) },
    excludeCredentials: (o.excludeCredentials || []).map((c: any) => ({ ...c, id: versOctets(c.id) })),
  };
}

export function optionsAssertion(o: any): PublicKeyCredentialRequestOptions {
  return {
    ...o,
    challenge: versOctets(o.challenge),
    allowCredentials: (o.allowCredentials || []).map((c: any) => ({ ...c, id: versOctets(c.id) })),
  };
}

/**
 * Résultats d'extensions envoyés au serveur, **sans la sortie PRF** (documents sensibles : elle dérive la clé de
 * chiffrement et ne quitte jamais le navigateur ; le serveur refuse toute assertion qui la contient).
 */
export function extensionsSansPrf(ext: AuthenticationExtensionsClientOutputs): Record<string, unknown> {
  const copie: any = { ...ext };
  if (copie.prf && typeof copie.prf === "object") {
    const { results: _sortie, ...reste } = copie.prf;
    void _sortie;
    copie.prf = reste;
  }
  return copie;
}

/** Sortie PRF (`first`, 32 octets) d'une assertion, copiée ; null si la passkey ne sait pas faire. */
export function sortiePrf(c: PublicKeyCredential): Uint8Array<ArrayBuffer> | null {
  const first = (c.getClientExtensionResults() as any)?.prf?.results?.first;
  if (!first) return null;
  const vue = first instanceof ArrayBuffer ? new Uint8Array(first) : new Uint8Array(first.buffer, first.byteOffset, first.byteLength);
  return vue.length >= 32 ? new Uint8Array(vue) : null;
}

export function credentialJson(c: PublicKeyCredential): Record<string, unknown> {
  const r = c.response as AuthenticatorAttestationResponse & AuthenticatorAssertionResponse;
  const sortie: any = {
    id: c.id,
    rawId: versB64(c.rawId),
    type: c.type,
    clientExtensionResults: extensionsSansPrf(c.getClientExtensionResults()),
    response: { clientDataJSON: versB64(r.clientDataJSON) },
  };
  if ("attestationObject" in r && r.attestationObject) {
    sortie.response.attestationObject = versB64(r.attestationObject);
    sortie.response.transports = typeof r.getTransports === "function" ? r.getTransports() : [];
  } else {
    sortie.response.authenticatorData = versB64(r.authenticatorData);
    sortie.response.signature = versB64(r.signature);
    if (r.userHandle) sortie.response.userHandle = versB64(r.userHandle);
  }
  return sortie;
}

export const passkeysDisponibles = (): boolean =>
  typeof window !== "undefined" && "PublicKeyCredential" in window && !!navigator.credentials;

/** Libellé lisible de l'appareil pour la liste des passkeys (jamais d'identifiant technique). */
export function nomAppareil(): string {
  const ua = navigator.userAgent;
  const systeme = ["Android", "iPhone", "iPad", "Windows", "Mac OS", "Linux"].find((s) => ua.includes(s)) ?? "appareil";
  return systeme.replace("Mac OS", "Mac");
}

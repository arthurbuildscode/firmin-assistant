// Notifications Web Push (lot 4b) : abonnement de cet appareil (clé VAPID du serveur), service worker /sw.js.
import { api } from "./api";

export const pushDisponible = (): boolean =>
  typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

function cleOctets(b64: string): Uint8Array<ArrayBuffer> {
  const normal = b64.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(b64.length / 4) * 4, "=");
  return Uint8Array.from(atob(normal), (c) => c.charCodeAt(0));
}

export async function abonnementActuel(): Promise<PushSubscription | null> {
  if (!pushDisponible()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/");
  return (await reg?.pushManager.getSubscription()) ?? null;
}

export async function activerNotifications(): Promise<void> {
  if (!pushDisponible()) throw new Error("Notifications non prises en charge par ce navigateur (sur iPhone : ajoute l’appli à l’écran d’accueil).");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("Notifications refusées dans les réglages du navigateur.");
  const { cle } = await api<{ cle: string }>("GET", "/api/push/cle");
  const reg = await navigator.serviceWorker.ready;
  const existant = await reg.pushManager.getSubscription();
  const abonnement = existant ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: cleOctets(cle) }));
  await api("POST", "/api/push/abonnement", abonnement.toJSON());
}

export async function couperNotifications(): Promise<void> {
  const abonnement = await abonnementActuel();
  if (!abonnement) return;
  await api("DELETE", "/api/push/abonnement", { endpoint: abonnement.endpoint }).catch(() => {});
  await abonnement.unsubscribe();
}

export function enregistrerServiceWorker(): void {
  if (!("serviceWorker" in navigator) || location.protocol === "http:" && location.hostname !== "localhost") return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
}

/** À la déconnexion : l'historique mis en cache pour la lecture hors ligne est effacé. */
export async function viderCacheHorsLigne(): Promise<void> {
  try {
    navigator.serviceWorker?.controller?.postMessage({ type: "vider" });
    const noms = await caches.keys();
    await Promise.all(noms.filter((n) => n.startsWith("homeos-donnees")).map((n) => caches.delete(n)));
  } catch {
    /* rien */
  }
}

// Service worker de l'appli (lot 4b) : appli en cache (ouverture hors ligne), lecture hors ligne de l'historique,
// notifications Web Push. Aucune écriture n'est rejouée hors ligne : seules les lectures sont servies du cache.
const VERSION = "v1";
const CACHE_APPLI = `homeos-appli-${VERSION}`;
const CACHE_DONNEES = "homeos-donnees";
const PAGES = ["/", "/manifest.webmanifest", "/icones/icone-192.png", "/icones/icone.svg"];
// Lectures gardées pour l'hors-ligne (jamais le coffre, les documents ni les captures).
const LECTURES = [/^\/auth\/session$/, /^\/api\/conversations$/, /^\/api\/conversations\/\d+\/messages$/, /^\/api\/brief\/jour$/, /^\/api\/routines$/];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE_APPLI);
      await cache.addAll(PAGES);
      // Fichiers à empreinte cités par la page (scripts, styles) : l'appli s'ouvre hors ligne dès la 1re visite.
      const page = await (await cache.match("/")).text();
      const ressources = [...new Set([...page.matchAll(/\/assets\/[A-Za-z0-9._-]+/g)].map((m) => m[0]))];
      await cache.addAll(ressources);
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const nom of await caches.keys()) if (nom.startsWith("homeos-appli-") && nom !== CACHE_APPLI) await caches.delete(nom);
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "vider") event.waitUntil(caches.delete(CACHE_DONNEES));
});

async function reseauPuisCache(requete, nomCache, cleCache) {
  const cache = await caches.open(nomCache);
  try {
    const reponse = await fetch(requete);
    if (reponse.ok) await cache.put(cleCache || requete, reponse.clone());
    else if (reponse.status === 401) await caches.delete(CACHE_DONNEES);
    return reponse;
  } catch (erreur) {
    const enCache = await cache.match(cleCache || requete);
    if (enCache) return enCache;
    throw erreur;
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.headers.get("accept") === "text/event-stream" || url.pathname.endsWith("/flux")) return;
  // Documents sensibles : jamais en cache (blobs chiffrés, trousseau), ni les URL blob: des documents déchiffrés.
  if (url.protocol === "blob:" || url.pathname.startsWith("/api/sensibles")) return;
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(caches.match(req).then((r) => r || fetch(req).then((rep) => {
      if (rep.ok) { const copie = rep.clone(); caches.open(CACHE_APPLI).then((c) => c.put(req, copie)); }
      return rep;
    })));
    return;
  }
  if (req.mode === "navigate") {
    // Toutes les pages de l'appli sont la même page : réseau d'abord, sinon la page en cache.
    event.respondWith(reseauPuisCache(req, CACHE_APPLI, "/"));
    return;
  }
  if (LECTURES.some((motif) => motif.test(url.pathname))) {
    event.respondWith(reseauPuisCache(req, CACHE_DONNEES));
  }
});

self.addEventListener("push", (event) => {
  let donnees = {};
  try {
    donnees = event.data ? event.data.json() : {};
  } catch {
    donnees = { texte: event.data ? event.data.text() : "" };
  }
  const titre = donnees.titre || "Nouveau message";
  event.waitUntil(
    self.registration.showNotification(titre, {
      body: donnees.texte || "",
      icon: "/icones/icone-192.png",
      badge: "/icones/badge-96.png",
      data: { url: donnees.url || "/" },
      tag: "homeos-message",
      renotify: true,
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const cible = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const fenetres = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const f of fenetres) {
        if (new URL(f.url).origin === self.location.origin) {
          await f.focus();
          f.postMessage({ type: "ouvrir", url: cible });
          return;
        }
      }
      await self.clients.openWindow(cible);
    })(),
  );
});

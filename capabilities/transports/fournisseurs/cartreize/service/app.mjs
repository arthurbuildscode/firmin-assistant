// Prochain bus — logique commune au serveur local (server.mjs) et à la fonction Vercel (api/index.mjs).
// Firmin Assistant : copie du projet d'origine ; seule différence, la redirection des anciens liens
// du prototype (un arrêt particulier) est retirée — aucune donnée personnelle dans le kit.
// - sert les pages web (dossier public/) en local
// - interroge les flux SIRI temps réel (avec cache, pour rester sous la limite)
// - croise avec la grille théorique (data/*.json, produits par prepare.py)
//
// Adresses :
//   /                              toutes les lignes
//   /l/<ligne>                     choix du sens et de l'arrêt
//   /l/<ligne>/<sens>/<arrêt>      prochains passages (c'est l'adresse des QR codes)
//   /qr?l=<ligne>[&s=<sens>&a=<arrêt>]   étiquettes QR à imprimer

import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const ICI = path.dirname(fileURLToPath(import.meta.url));
const CACHE_MS = 60_000;            // une réponse SIRI est réutilisée 60 s par quai (quota serré)
const MAX_APPELS_PAR_MINUTE = 8;    // limite officielle : 10-20/min, on reste en dessous

const lireJson = (f) => JSON.parse(fs.readFileSync(path.join(ICI, f), "utf8"));
const config = lireJson("config.json");
if (process.env.SIRI_BASE) config.siriBase = process.env.SIRI_BASE; // utile pour tester une panne
const reseau = lireJson("data/reseau.json");
const horaires = lireJson("data/horaires.json");
const lignes = Object.fromEntries(reseau.lignes.map((l) => [l.id, l]));
// "ligne/n° de course" -> sens GTFS, pour trier les bus du temps réel à un quai partagé
const sensCourse = new Map(horaires.courses.filter((c) => c[1]).map((c) => [`${c[0]}/${c[1]}`, c[4]]));

// arrêt (slug) -> [{ ligne, sens, arret }] pour proposer les autres lignes au même arrêt
const parArret = new Map();
for (const l of reseau.lignes)
  for (const s of l.sens)
    for (const a of s.arrets) {
      if (!parArret.has(a.id)) parArret.set(a.id, []);
      parArret.get(a.id).push({ ligne: l, sens: s, arret: a });
    }

// ---------- Heure de Paris ----------

const fmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Paris", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", weekday: "short",
});
const JOURS = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };

function paris(ms) {
  const p = Object.fromEntries(fmt.formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return {
    ymd: `${p.year}${p.month}${p.day}`,
    jour: JOURS[p.weekday],
    min: Number(p.hour) * 60 + Number(p.minute) + Number(p.second) / 60,
  };
}

// Minutes depuis minuit (heure de Paris) du jour `ref`, pour un instant donné.
function minutesDepuis(ref, ms) {
  const p = paris(ms);
  if (p.ymd === ref.ymd) return p.min;
  return p.ymd > ref.ymd ? p.min + 1440 : p.min - 1440;
}

const hhmm = (m) => {
  const x = ((Math.round(m) % 1440) + 1440) % 1440;
  return `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")}`;
};

function serviceActif(id, ymd, jour) {
  const s = horaires.services[id];
  if (!s) return false;
  if (s.ajout.includes(ymd)) return true;
  if (s.retrait.includes(ymd)) return false;
  return s.debut <= ymd && ymd <= s.fin && s.jours[jour];
}

// Passages théoriques d'une ligne à un quai : hier (après minuit), aujourd'hui et demain,
// en minutes relatives à aujourd'hui.
function passagesTheoriques(point, ligne, direction, maintenant) {
  const res = [];
  for (const decalage of [-1, 0, 1]) {
    const j = paris(maintenant + decalage * 86_400_000);
    for (const [idx, min] of horaires.passages[point] || []) {
      const [l, course, service, terminus, dir] = horaires.courses[idx];
      if (l !== ligne || dir !== direction || !serviceActif(service, j.ymd, j.jour)) continue;
      res.push({ ligne: l, course, terminus: horaires.terminus[terminus], min: min + decalage * 1440 });
    }
  }
  return res.sort((a, b) => a.min - b.min);
}

// ---------- SIRI ----------

const cache = new Map();   // "flux/quai" -> { quand, ok, passages, erreur }
const enCours = new Map(); // "flux/quai" -> Promise
const appels = [];         // horodatages des derniers appels SIRI
const demarrage = Date.now();
const dernieresErreurs = []; // 20 dernières erreurs SIRI de cette instance, pour /api/diagnostic

// Une seule requête peut interroger plusieurs quais (un StopMonitoringRequest par quai).
function requeteSiri(monitoringRefs) {
  const ts = new Date().toISOString();
  return `<?xml version="1.0" encoding="UTF-8"?>
<S:Envelope xmlns:S="http://schemas.xmlsoap.org/soap/envelope/" xmlns:sw="http://wsdl.siri.org.uk" xmlns:siri="http://www.siri.org.uk/siri">
<S:Body><sw:GetSiriService><Request version="2.0">
<siri:RequestTimestamp>${ts}</siri:RequestTimestamp>
<siri:RequestorRef>${config.requestorRef}</siri:RequestorRef>
<siri:MessageIdentifier>${randomUUID()}</siri:MessageIdentifier>
${monitoringRefs.map((ref) => `<siri:StopMonitoringRequest version="2.0">
<siri:RequestTimestamp>${ts}</siri:RequestTimestamp>
<siri:MessageIdentifier>${randomUUID()}</siri:MessageIdentifier>
<siri:PreviewInterval>PT3H</siri:PreviewInterval>
<siri:MonitoringRef>${ref}</siri:MonitoringRef>
</siri:StopMonitoringRequest>`).join("")}
</Request></sw:GetSiriService></S:Body></S:Envelope>`;
}

const champ = (xml, nom) => {
  const m = xml.match(new RegExp(`<(?:\\w+:)?${nom}>([^<]*)</(?:\\w+:)?${nom}>`));
  return m ? m[1] : null;
};

function lirePassagesSiri(xml) {
  const res = [];
  for (const [, v] of xml.matchAll(/<(?:\w+:)?MonitoredStopVisit>([\s\S]*?)<\/(?:\w+:)?MonitoredStopVisit>/g)) {
    const course = (champ(v, "DatedVehicleJourneyRef") || "").match(/VehicleJourney::([^:]+)/);
    const prevu = champ(v, "AimedDepartureTime") || champ(v, "AimedArrivalTime");
    const estime = champ(v, "ExpectedDepartureTime") || champ(v, "ExpectedArrivalTime");
    res.push({
      ligne: champ(v, "PublishedLineName") || (champ(v, "LineRef") || "").replace(/^.*Line::([^:]+).*$/, "$1"),
      course: course ? course[1] : "",
      destination: champ(v, "DestinationName") || "",
      suivi: champ(v, "Monitored") === "true",
      aLArret: champ(v, "VehicleAtStop") === "true",
      statut: champ(v, "DepartureStatus") || champ(v, "ArrivalStatus") || "",
      prevuMs: prevu ? Date.parse(prevu) : null,
      estimeMs: estime ? Date.parse(estime) : null,
    });
  }
  return res;
}

// Si un flux échoue plusieurs fois de suite (ex. connexion coupée depuis l'hébergeur), on le laisse
// de côté une minute : les pages s'affichent tout de suite en horaires théoriques.
const ECHECS_AVANT_PAUSE = 3;
const PAUSE_APRES_PANNE_MS = 60_000;
const pannes = new Map(); // flux -> fin de la pause
const echecs = new Map(); // flux -> échecs consécutifs

// Réponse : un StopMonitoringDelivery par quai demandé, dans le même ordre.
function lireLivraisons(xml) {
  return [...xml.matchAll(/<(?:\w+:)?StopMonitoringDelivery[\s>][\s\S]*?<\/(?:\w+:)?StopMonitoringDelivery>/g)]
    .map(([d]) => lirePassagesSiri(d));
}

async function interrogerSiri(flux, points) {
  if ((pannes.get(flux) || 0) > Date.now()) throw new Error("temps réel indisponible (nouvel essai dans quelques minutes)");
  try {
    // Depuis l'hébergeur, certains appels restent sans réponse alors que les autres répondent
    // en moins d'une seconde : délai court et un second essai.
    const res = await appelSiri(flux, points).catch((e) => {
      if (/limite de requêtes|HTTP 429/.test(e.message)) throw e;
      return appelSiri(flux, points);
    });
    echecs.set(flux, 0);
    return res;
  } catch (e) {
    if (!/limite de requêtes|HTTP 429/.test(e.message)) {
      const n = (echecs.get(flux) || 0) + 1;
      echecs.set(flux, n);
      if (n >= ECHECS_AVANT_PAUSE) { pannes.set(flux, Date.now() + PAUSE_APRES_PANNE_MS); echecs.set(flux, 0); }
    }
    throw e;
  }
}

async function appelSiri(flux, points) {
  const maintenant = Date.now();
  while (appels.length && appels[0] < maintenant - 60_000) appels.shift();
  // le flux compte chaque quai d'une requête groupée comme une requête
  if (appels.length + points.length > MAX_APPELS_PAR_MINUTE) throw new Error("limite de requêtes atteinte, réessai bientôt");
  for (const _ of points) appels.push(maintenant);
  const rep = await fetch(config.siriBase + flux, {
    method: "POST",
    headers: { "Content-Type": "text/xml; charset=utf-8", SOAPAction: "" },
    body: requeteSiri(points.map((p) => `${config.tempsReel[flux].prefixe}:StopPoint:BP:${p}:LOC`)),
    signal: AbortSignal.timeout(Number(process.env.SIRI_DELAI_MS || 3_000)),
  });
  const xml = await rep.text();
  if (!rep.ok) throw new Error(`SIRI HTTP ${rep.status} : ${champ(xml, "faultstring") || ""}`.trim());
  if (/AllowedResourceUsageExceeded/.test(xml)) throw new Error("limite de requêtes atteinte côté SIRI");
  if (champ(xml, "Status") === "false") throw new Error("SIRI a répondu Status=false");
  const livraisons = lireLivraisons(xml);
  if (livraisons.length !== points.length) throw new Error(`SIRI : ${livraisons.length} réponses pour ${points.length} quais`);
  return livraisons;
}

// Met à jour le cache des quais demandés qui ne sont plus frais, en un seul appel SIRI.
async function chargerQuais(flux, points) {
  if (!flux) return;
  const maintenant = Date.now();
  const aCharger = points.filter((p) => {
    const c = cache.get(`${flux}/${p}`);
    return !c || maintenant - c.quand >= CACHE_MS;
  });
  if (!aCharger.length) return;
  const cleAppel = `${flux}/${aCharger.join(",")}`;
  if (!enCours.has(cleAppel)) {
    enCours.set(cleAppel, interrogerSiri(flux, aCharger)
      .then((livraisons) => {
        const t = Date.now();
        aCharger.forEach((p, i) => {
          const cle = `${flux}/${p}`;
          const c = cache.get(cle);
          let passages = livraisons[i];
          let quandDonnees = t;
          // Le flux « clignote » : un même quai répond parfois vide quelques secondes après avoir listé
          // les bus. On garde alors la dernière liste non vide (moins de 3 min), sans les bus déjà passés.
          if (!passages.length && c?.ok && c.passages.length && t - c.quandDonnees < 180_000) {
            passages = c.passages.filter((x) => (x.estimeMs ?? x.prevuMs ?? 0) > t - 60_000);
            quandDonnees = c.quandDonnees;
          }
          cache.set(cle, { quand: t, quandDonnees, ok: true, passages, erreur: null, essai: t, reponse: t });
        });
      })
      .catch((e) => {
        console.error(`[SIRI ${cleAppel}]`, e.message);
        dernieresErreurs.push({ quand: new Date().toISOString(), appel: cleAppel, erreur: e.message });
        if (dernieresErreurs.length > 20) dernieresErreurs.shift();
        const t = Date.now();
        for (const p of aCharger) {
          const cle = `${flux}/${p}`;
          const c = cache.get(cle);
          // On garde l'ancienne réponse si elle a moins de 5 min (nouvel essai dans 60 s), sinon on passe en théorique.
          if (c && c.ok && t - c.quandDonnees < 300_000) cache.set(cle, { ...c, quand: t, erreur: e.message, essai: t });
          // Sans donnée de secours, on retente au bout de 15 s (60 s si c'est le quota qui bloque).
          else cache.set(cle, { quand: /limite de requêtes|HTTP 429/.test(e.message) ? t : t - CACHE_MS + 15_000, quandDonnees: 0, ok: false, passages: [], erreur: e.message, essai: t, reponse: c?.reponse || 0 });
        }
      })
      .finally(() => enCours.delete(cleAppel)));
  }
  return enCours.get(cleAppel);
}

function tempsReel(flux, point) {
  if (!flux) return { quand: Date.now(), ok: false, passages: [], erreur: "pas de temps réel pour cette ligne", absent: true };
  return cache.get(`${flux}/${point}`) || { quand: Date.now(), ok: false, passages: [], erreur: "pas encore interrogé" };
}

// ---------- Croisement théorique / temps réel ----------

// Le flux renvoie parfois une liste vide pour un quai alors que les arrêts suivants de la ligne ont
// bien les bus (constaté sur certains arrêts). Dans ce cas, on reprend l'avance ou le
// retard de chaque bus à l'arrêt suivant qui a des données.
const NB_ARRETS_SUIVANTS = 2;
const arretsSuivants = (sens, arret) => {
  const i = sens.arrets.findIndex((a) => a.id === arret.id);
  return sens.arrets.slice(i + 1, i + 1 + NB_ARRETS_SUIVANTS);
};

function retardsArretsSuivants(ligne, sens, arret) {
  for (const suivant of arretsSuivants(sens, arret)) {
    for (const point of suivant.points) {
      const rt = tempsReel(ligne.flux, point);
      const visites = rt.ok ? rt.passages.filter((x) => x.ligne === ligne.ligne && x.course && x.prevuMs && x.estimeMs) : [];
      if (visites.length)
        return new Map(visites.map((x) => [x.course, { retard: (x.estimeMs - x.prevuMs) / 60_000, suivi: x.suivi }]));
    }
  }
  return null;
}

// Dernier retard connu par course (jour/course -> { retard en min, quand }) : si un bus suivi disparaît
// du flux avant son heure, on garde son heure la plus plausible (fiche + dernier retard).
// En mémoire de la fonction, et recopié dans une base Redis partagée si elle est branchée (Vercel fait
// tourner plusieurs copies du serveur, chacune avec sa mémoire, perdue à chaque redémarrage).
const derniersRetards = new Map();
const SIGNAL_VALABLE_MS = 3 * 3600_000;

// ---------- Mémoire partagée des retards (Upstash Redis via son API REST, facultative) ----------
// Noms posés par l'intégration Vercel : <PRÉFIXE>_REST_API_URL / _TOKEN (préfixe KV par défaut), ou ceux d'Upstash.
const envFin = (fin) => process.env[`KV${fin}`] ?? Object.entries(process.env).find(([k]) => k.endsWith(fin))?.[1];
const redisUrl = envFin("_REST_API_URL") || process.env.UPSTASH_REDIS_REST_URL;
const redisJeton = envFin("_REST_API_TOKEN") || process.env.UPSTASH_REDIS_REST_TOKEN;
async function redis(commandes) {
  if (!redisUrl || !redisJeton) return null;
  try {
    const r = await fetch(`${redisUrl}/pipeline`, {
      method: "POST",
      headers: { Authorization: `Bearer ${redisJeton}`, "Content-Type": "application/json" },
      body: JSON.stringify(commandes),
      signal: AbortSignal.timeout(1000),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return (await r.json()).map((x) => x.result);
  } catch (e) {
    console.error("[redis]", e.message);
    dernieresErreurs.push({ quand: new Date().toISOString(), appel: "redis", erreur: e.message });
    if (dernieresErreurs.length > 20) dernieresErreurs.shift();
    return null;
  }
}
// Une clé de hachage par jour et par ligne : course -> { retard, quand, vu }
const cleRedis = (ymd, ligne) => `retards:${ymd}:${ligne}`;
const lusPartages = new Map(); // dernière lecture par jour/ligne (au plus une toutes les 20 s par copie)
async function lireRetardsPartages(ymd, ligne) {
  const cle = cleRedis(ymd, ligne);
  if (Date.now() - (lusPartages.get(cle) || 0) < 20_000) return;
  lusPartages.set(cle, Date.now());
  const h = (await redis([["HGETALL", cle]]))?.[0];
  if (!Array.isArray(h)) return;
  for (let i = 0; i + 1 < h.length; i += 2) {
    try {
      const v = JSON.parse(h[i + 1]);
      const m = derniersRetards.get(`${ymd}/${h[i]}`);
      if (!m || (v.vu || 0) > (m.vu || 0)) derniersRetards.set(`${ymd}/${h[i]}`, { ...v, ecrit: v.vu });
    } catch {}
  }
}
const aEcrire = []; // [clé redis, course, valeur] en attente
async function ecrireRetardsPartages() {
  if (!aEcrire.length || !redisUrl) { aEcrire.length = 0; return; }
  const parCle = new Map();
  for (const [cle, course, v] of aEcrire.splice(0)) {
    if (!parCle.has(cle)) parCle.set(cle, []);
    parCle.get(cle).push(course, JSON.stringify(v));
  }
  await redis([...parCle].flatMap(([cle, champs]) => [["HSET", cle, ...champs], ["EXPIRE", cle, 86_400]]));
}

function listeQuai(ligne, sens, arret, point, ref) {
  const maintenant = Date.now();
  const theo = passagesTheoriques(point, ligne.ligne, sens.direction, maintenant);
  const rt = tempsReel(ligne.flux, point);
  // à un quai partagé par les deux sens (terminus), on écarte les courses de l'autre sens
  const restants = new Set(rt.passages.filter((x) => x.ligne === ligne.ligne
    && (!x.course || (sensCourse.get(`${ligne.ligne}/${x.course}`) ?? sens.direction) === sens.direction)));
  const sansDonnees = rt.ok && restants.size === 0;
  const voisins = sansDonnees ? retardsArretsSuivants(ligne, sens, arret) : null;
  const liste = [];
  for (const p of theo) {
    if (p.min < ref.min - 180 || p.min > ref.min + 1440) continue;
    let v = [...restants].find((x) => (p.course && x.course === p.course)
      || (x.prevuMs && Math.abs(minutesDepuis(ref, x.prevuMs) - p.min) < 1));
    if (v) restants.delete(v);
    const d = !v && voisins?.get(p.course);
    if (d) { // passage reconstitué à partir de l'arrêt suivant
      const prevuMs = maintenant + (p.min - ref.min) * 60_000;
      v = { prevuMs, estimeMs: prevuMs + d.retard * 60_000, suivi: d.suivi, aLArret: false, statut: "", deduit: true };
    }
    const cleSignal = `${ref.ymd}/${p.course}`;
    if (v && p.course && v.prevuMs != null && v.estimeMs != null) {
      const avant = derniersRetards.get(cleSignal);
      const n = { retard: (v.estimeMs - v.prevuMs) / 60_000, quand: maintenant, vu: rt.quandDonnees || rt.reponse || maintenant };
      // recopie partagée si le retard a changé, sinon au plus toutes les 2 min
      const partager = !avant || avant.retard !== n.retard || n.vu - (avant.ecrit || 0) > 120_000;
      n.ecrit = partager ? n.vu : avant.ecrit;
      derniersRetards.set(cleSignal, n);
      if (partager) aEcrire.push([cleRedis(ref.ymd, ligne.ligne), p.course, { retard: n.retard, quand: n.quand, vu: n.vu }]);
    }
    const signal = derniersRetards.get(cleSignal);
    const r = fusion(p.ligne, p.terminus, p.min, v, rt.ok, ref,
      signal && maintenant - signal.quand < SIGNAL_VALABLE_MS ? signal.retard : null);
    r.course = p.course;
    // heure (ms) de la dernière position reçue pour ce bus, pour « il y a … » sur la page
    if (v || r.signalPerdu) r.vuA = signal?.vu || null;
    if (r.statut === "prevu" && sansDonnees) r.sansDonnees = true;
    liste.push(r);
  }
  for (const v of restants) { // bus présents en temps réel mais absents de la grille
    if (v.prevuMs) liste.push(fusion(v.ligne, v.destination, minutesDepuis(ref, v.prevuMs), v, true, ref));
  }
  return { liste, rt, sansDonnees, deduit: !!voisins };
}

function fusion(ligne, destination, minTheo, v, siriOk, ref, dernierRetard = null) {
  const r = { ligne, destination: destination || v?.destination || "", prevu: hhmm(minTheo), estime: null, retard: null, dans: null, suivi: false, statut: "" };
  if (v) {
    const estime = v.estimeMs != null ? minutesDepuis(ref, v.estimeMs) : minTheo;
    const prevuSiri = v.prevuMs != null ? minutesDepuis(ref, v.prevuMs) : minTheo;
    r.estime = hhmm(estime);
    r.retard = Math.round(estime - prevuSiri);
    r.dans = Math.max(0, Math.round(estime - ref.min));
    r.suivi = v.suivi;
    r.statut = /cancel/i.test(v.statut) ? "supprime" : v.aLArret ? "a_l_arret" : "a_venir";
    if (v.deduit) {
      r.deduit = true;
      if (estime < ref.min - 1) r.statut = "passe"; // déjà passé ici, pas encore à l'arrêt suivant
    }
    r._tri = estime;
  } else if (dernierRetard != null) {
    // signal perdu : heure la plus plausible = fiche + dernier retard connu
    const estime = minTheo + dernierRetard;
    r.estime = hhmm(estime);
    r.retard = Math.round(dernierRetard);
    r.dans = Math.max(0, Math.round(estime - ref.min));
    r.statut = estime < ref.min - 1 ? "passe" : "a_venir";
    r.signalPerdu = r.statut === "a_venir";
    r._tri = estime;
  } else {
    r.dans = Math.round(minTheo - ref.min);
    r.statut = minTheo < ref.min ? (siriOk ? "passe" : "passe_theorique") : "prevu";
    r._tri = minTheo;
  }
  return r;
}

const resumeLigne = (l) => ({ id: l.id, ligne: l.ligne, nom: l.nom, couleur: l.couleur, texte: l.texte, tempsReel: l.tempsReel });

async function etatArret(ligne, sens, arret) {
  const maintenant = Date.now();
  const ref = paris(maintenant);
  // Le quota SIRI est d'environ 10 quais par minute : on n'interroge que les quais de l'arrêt, et les
  // arrêts suivants seulement si le flux ne donne rien ici (en un seul appel groupé).
  await Promise.all([chargerQuais(ligne.flux, arret.points), lireRetardsPartages(ref.ymd, ligne.ligne)]);
  const vide = (p) => { const rt = tempsReel(ligne.flux, p); return rt.ok && !rt.passages.some((x) => x.ligne === ligne.ligne); };
  if (arret.points.some(vide))
    await chargerQuais(ligne.flux, [...new Set(arretsSuivants(sens, arret).flatMap((a) => a.points))]);
  const res = arret.points.map((p) => listeQuai(ligne, sens, arret, p, ref));
  await ecrireRetardsPartages();
  const liste = res.flatMap((r) => r.liste).sort((a, b) => a._tri - b._tri);
  const rts = res.map((r) => r.rt);

  const estPasse = (x) => x.statut === "passe" || x.statut === "passe_theorique";
  const passes = liste.filter(estPasse).slice(-3);
  const prochains = liste.filter((x) => !estPasse(x)).slice(0, 40); // la page en montre quelques-uns, puis « Afficher plus »
  for (const x of liste) delete x._tri;

  // Même arrêt dans l'autre sens, et autres lignes qui s'y arrêtent
  const autresSens = ligne.sens.map((s) => {
    const a = s.arrets.find((x) => x.id === arret.id);
    // à un terminus, on ne peut pas monter dans l'autre sens : pas de lien (onglet désactivé)
    return { id: s.id, libelle: s.libelle, url: a ? `/l/${ligne.id}/${s.id}/${a.id}` : null };
  });
  const autresLignes = [];
  for (const x of parArret.get(arret.id) || []) {
    if (x.ligne.id === ligne.id || autresLignes.some((y) => y.id === x.ligne.id)) continue;
    // on privilégie le sens qui partage un quai avec celui-ci, sinon celui qui va vers la même ville
    const candidats = (parArret.get(arret.id) || []).filter((y) => y.ligne.id === x.ligne.id);
    const memeQuai = candidats.find((y) => y.arret.points.some((p) => arret.points.includes(p)))
      || candidats.find((y) => y.sens.libelle === sens.libelle) || x;
    autresLignes.push({ ...resumeLigne(x.ligne), url: `/l/${x.ligne.id}/${memeQuai.sens.id}/${arret.id}`, sens: memeQuai.sens.libelle });
  }

  return {
    ligne: resumeLigne(ligne),
    sens: { id: sens.id, libelle: sens.libelle },
    arret: { id: arret.id, nom: arret.nom, commune: arret.commune },
    autresSens,
    autresLignes,
    heure: hhmm(ref.min),
    tempsReel: {
      ok: rts.every((r) => r.ok),
      disponible: !!ligne.flux,
      ageSecondes: Math.max(0, Math.round((Date.now() - Math.min(...rts.map((r) => r.quand))) / 1000)),
      erreur: rts.map((r) => r.erreur).filter(Boolean)[0] || null,
      // horodatages (ms) pour « actualisé il y a … » côté page : dernière réponse du flux, dernière tentative
      reponseA: rts.every((r) => r.reponse) ? Math.min(...rts.map((r) => r.reponse)) : null,
      essaiA: Math.max(0, ...rts.map((r) => r.essai || 0)) || null,
      // le flux répond mais ne donne rien pour cet arrêt (on estime alors via l'arrêt suivant si possible)
      arretSansDonnees: res.every((r) => r.sansDonnees),
      estimeViaArretSuivant: res.some((r) => r.deduit),
    },
    passes,
    prochains,
  };
}

// ---------- Serveur HTTP ----------

const TYPES = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml", ".json": "application/json" };

function envoyerFichier(res, fichier) {
  fs.readFile(path.join(ICI, "public", fichier), (err, data) => {
    if (err) { res.writeHead(404); return res.end("Introuvable"); }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(fichier)] || "application/octet-stream" });
    res.end(data);
  });
}

const json = (res, code, obj, cache = "no-store") => {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": cache });
  res.end(JSON.stringify(obj));
};

export async function gerer(req, res) {
  const url = new URL(req.url, "http://x");
  // Sur Vercel, les adresses sont réécrites vers /api/index?chemin=<adresse d'origine>
  const p = url.searchParams.get("chemin") || url.pathname;
  let m;
  try {
    if (p === "/sante") return json(res, 200, { ok: true });
    if (p === "/api/diagnostic") {
      // état de l'instance qui répond (chaque instance Vercel a son propre cache et ses compteurs)
      const t = Date.now();
      return json(res, 200, {
        instanceDepuisSecondes: Math.round((t - demarrage) / 1000),
        appelsDerniereMinute: appels.filter((x) => x > t - 60_000).length,
        memoirePartagee: !!(redisUrl && redisJeton), variablesStockage: Object.keys(process.env).filter((k) => /REST_API|REDIS|^KV_/.test(k)), fluxEnPause: [...pannes].filter(([, fin]) => fin > t).map(([flux]) => flux),
        dernieresErreurs,
      });
    }
    if (p === "/api/lignes") return json(res, 200, reseau.lignes.map(resumeLigne));
    if (p === "/api/info") {
      // adresse publique (tunnel ou hébergement), écrite dans data/url-publique.txt
      let urlPublique = process.env.URL_PUBLIQUE || "";
      try { urlPublique ||= fs.readFileSync(path.join(ICI, "data", "url-publique.txt"), "utf8").trim(); } catch {}
      return json(res, 200, { urlPublique });
    }
    if ((m = p.match(/^\/api\/ligne\/([\w-]+)$/))) {
      const l = lignes[m[1]];
      if (!l) return json(res, 404, { erreur: "ligne inconnue" });
      return json(res, 200, { ...resumeLigne(l), sens: l.sens.map((s) => ({ id: s.id, libelle: s.libelle, arrets: s.arrets.map(({ id, nom, commune }) => ({ id, nom, commune })) })) });
    }
    if ((m = p.match(/^\/api\/passages\/([\w-]+)\/([\w-]+)\/([\w-]+)$/))) {
      const l = lignes[m[1]];
      const s = l?.sens.find((x) => x.id === m[2]);
      const a = s?.arrets.find((x) => x.id === m[3]);
      if (!a) return json(res, 404, { erreur: "arrêt inconnu" });
      // Le CDN de Vercel garde la réponse 60 s : un seul appel SIRI pour tous les visiteurs.
      // Une réponse sans temps réel n'est gardée que 10 s, sinon elle resservirait l'échec à l'ouverture.
      const etat = await etatArret(l, s, a);
      const garde = etat.tempsReel.disponible && !etat.tempsReel.ok
        ? "public, max-age=0, s-maxage=10"
        : "public, max-age=0, s-maxage=60, stale-while-revalidate=300";
      return json(res, 200, etat, garde);
    }
    if (p === "/") return envoyerFichier(res, "index.html");
    if (p === "/qr") return envoyerFichier(res, "qr.html");
    if (/^\/l\/[\w-]+\/?$/.test(p)) return envoyerFichier(res, "ligne.html");
    if (/^\/l\/[\w-]+\/[\w-]+\/[\w-]+$/.test(p)) return envoyerFichier(res, "arret.html");
    if (/^\/[\w.-]+\.(css|js|svg)$/.test(p)) return envoyerFichier(res, p.slice(1));
    res.writeHead(404); res.end("Introuvable");
  } catch (e) {
    console.error(e);
    json(res, 500, { erreur: e.message });
  }
}

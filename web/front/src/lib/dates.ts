// Dates en français, courtes (choix d'affichage : jamais de date en toutes lettres dans l'en-tête).

const JOURS = ["dim.", "lun.", "mar.", "mer.", "jeu.", "ven.", "sam."];
const JOURS_LONGS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"];
const MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."];

export const pad = (n: number) => String(n).padStart(2, "0");

/** « 2026-11-07 » → Date locale à minuit (sans décalage UTC). */
export function dateLocale(iso: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return null;
  return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}

export function isoJour(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function ajouterJours(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

export const jourCourt = (d: Date) => `${JOURS[d.getDay()]} ${d.getDate()} ${MOIS[d.getMonth()]}`;
export const jourMoisCourt = (d: Date) => `${JOURS[d.getDay()]} ${d.getDate()}`;
export const nomJour = (d: Date) => JOURS_LONGS[d.getDay()];
export const nomJourCourt = (d: Date) => JOURS[d.getDay()];
export const moisCourt = (d: Date) => MOIS[d.getMonth()];

/** Heure « 09:30 » d'un horodatage ISO, ou "" pour une date seule. */
export function heure(iso: string | null | undefined): string {
  const m = /T(\d{2}):(\d{2})/.exec(iso || "");
  return m ? `${m[1]}:${m[2]}` : "";
}

export function heureDe(ts: string | undefined): string {
  if (!ts) return "";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? heure(ts) : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Libellé relatif d'un jour : « ce soir » n'est pas déductible, on reste sur aujourd'hui / demain / jeu. 12. */
export function jourRelatif(iso: string, aujourdhui = new Date()): string {
  const d = dateLocale(iso);
  if (!d) return iso || "";
  const ecart = Math.round((d.getTime() - dateLocale(isoJour(aujourdhui))!.getTime()) / 864e5);
  if (ecart === 0) return "aujourd’hui";
  if (ecart === 1) return "demain";
  if (ecart > 1 && ecart < 7) return jourMoisCourt(d);
  if (ecart === -1) return "hier";
  if (ecart < -1 && ecart > -7) return nomJourCourt(d);
  return `${d.getDate()} ${MOIS[d.getMonth()]}`;
}

export function duree(ms: number | undefined): string {
  if (!ms || ms < 0) return "";
  const s = Math.max(1, Math.round(ms / 1000));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${pad(s % 60)}`;
}

export function chrono(s: number): string {
  return `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`;
}

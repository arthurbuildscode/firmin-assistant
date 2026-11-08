// Réglages : propres à la personne et à cet appareil (palette, thème, voix, notifications), appareils et passkeys,
// état du système (propriétaires). Le prénom de l'assistant est lu dans la configuration du foyer.
import { useEffect, useState } from "react";
import { Fingerprint, LogOut, Smartphone, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { BoutonEcouter } from "@/components/cartes/actions";
import { api, inscrire, messageErreur } from "@/lib/api";
import { heureDe, jourCourt } from "@/lib/dates";
import { modifierPreferences, PALETTES, themeEffectif, usePreferences, type CleTheme } from "@/lib/preferences";
import { abonnementActuel, activerNotifications, couperNotifications, pushDisponible } from "@/lib/push";
import { naviguer } from "@/lib/routeur";
import { rafraichirSession, useAssistant, useSession } from "@/lib/session";
import { toast } from "@/lib/toasts";
import { voixNavigateurDisponibles } from "@/lib/voix";
import { passkeysDisponibles } from "@/lib/webauthn";
import { TitrePage } from "./commun";

interface Appareils {
  sessions: {
    id: number;
    appareil: string;
    cree: string;
    vu: string;
    methode: string;
    courante: boolean;
    niveau?: "passkey" | "secours";
    tailscale_login?: string | null;
  }[];
  passkeys: { id: number; nom: string; cree: string; utilise: string | null }[];
  /** Compte Tailscale fixé dans la configuration du foyer (sinon : appris à la connexion). */
  tailscale_attendu?: string | null;
}

/** « Vu lun. 9 · 08:12 · vu depuis alex@exemple.org · connexion de secours » */
export function detailSession(s: Appareils["sessions"][number], vu: string): string {
  return [
    `Vu ${vu}`,
    s.tailscale_login ? `vu depuis ${s.tailscale_login}` : "",
    s.niveau === "secours" ? "connexion de secours (24 h)" : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

function quand(ts: string | null | undefined): string {
  if (!ts) return "jamais";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : `${jourCourt(d)} · ${heureDe(ts)}`;
}

function Reglage({ titre, sous, children }: { titre: string; sous?: string; children: React.ReactNode }) {
  return (
    <div className="reglage">
      <div className="lbl">
        <div className="ligne-t">{titre}</div>
        {sous && <div className="ligne-s">{sous}</div>}
      </div>
      {children}
    </div>
  );
}

function Notifications() {
  const [actif, setActif] = useState(false);
  const [occupe, setOccupe] = useState(false);
  useEffect(() => {
    abonnementActuel().then((a) => setActif(!!a)).catch(() => {});
  }, []);
  if (!pushDisponible())
    return (
      <Reglage titre="Notifications" sous="Sur iPhone, ajoute d’abord l’appli à l’écran d’accueil (Partager → Sur l’écran d’accueil).">
        <span className="text-sm text-muted-foreground">Indisponibles ici</span>
      </Reglage>
    );
  return (
    <Reglage titre="Notifications sur cet appareil" sous="Brief, rappels et résultats des routines quand l’appli est fermée.">
      <Switch
        checked={actif}
        disabled={occupe}
        aria-label="Notifications sur cet appareil"
        onCheckedChange={async (v) => {
          setOccupe(true);
          try {
            if (v) await activerNotifications();
            else await couperNotifications();
            setActif(v);
            toast(v ? "Notifications activées." : "Notifications coupées.");
          } catch (e) {
            toast(messageErreur(e));
          } finally {
            setOccupe(false);
          }
        }}
      />
    </Reglage>
  );
}

function Etat() {
  const [etat, setEtat] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    api<Record<string, unknown>>("GET", "/api/etat").then(setEtat).catch(() => setEtat(null));
  }, []);
  if (!etat) return null;
  const caps = (etat.capacites as { titre: string; statut: string; derniere_verification?: { ok?: boolean; message?: string } }[]) ?? [];
  const sauvegarde = (etat.sauvegarde as { derniere?: string; copie_hors_site?: string } | undefined) ?? {};
  const statuts: Record<string, string> = { actif: "active", essai: "en essai", inactif: "désactivée", invalide: "à corriger" };
  const lignes: [string, string, boolean][] = [
    ...caps.map((c): [string, string, boolean] => [
      c.titre,
      [statuts[c.statut] ?? c.statut, c.derniere_verification ? (c.derniere_verification.ok ? "vérifiée" : `en échec : ${c.derniere_verification.message ?? ""}`) : ""].filter(Boolean).join(" · "),
      c.statut !== "invalide" && c.derniere_verification?.ok !== false,
    ]),
    ["Dernière sauvegarde", sauvegarde.derniere ? String(sauvegarde.derniere) : "aucune", !!sauvegarde.derniere],
    ["Copie hors site", sauvegarde.copie_hors_site ? String(sauvegarde.copie_hors_site) : "aucune", !!sauvegarde.copie_hors_site],
  ];
  const ok = lignes.every((l) => l[2]) && !etat.pause;
  return (
    <section className="section">
      <div className="section-t">
        <h2>Statut</h2>
        <span className="text-sm text-muted-foreground">{etat.pause ? "En pause" : ok ? "Tout fonctionne" : "À regarder"}</span>
      </div>
      <div className="liste rounded-lg border px-4">
        {lignes.map(([t, s, bon]) => (
          <div className="ligne" key={t}>
            <span className="size-[7px] flex-none rounded-full" style={{ background: bon ? "var(--good)" : "var(--warn)" }} aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="ligne-t">{t}</div>
              <div className="ligne-s">{s}</div>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function EcranReglages({ onDeconnexion }: { onDeconnexion: () => void }) {
  const session = useSession();
  const assistant = useAssistant();
  const prefs = usePreferences();
  const [appareils, setAppareils] = useState<Appareils | null>(null);
  const [voix, setVoix] = useState<SpeechSynthesisVoice[]>([]);
  const sombre = themeEffectif() === "dark";

  const chargerAppareils = () => api<Appareils>("GET", "/api/appareils").then(setAppareils).catch((e) => toast(messageErreur(e)));
  useEffect(() => {
    void chargerAppareils();
    const v = () => setVoix(voixNavigateurDisponibles());
    v();
    window.speechSynthesis?.addEventListener?.("voiceschanged", v);
    return () => window.speechSynthesis?.removeEventListener?.("voiceschanged", v);
  }, []);

  return (
    <>
      <TitrePage titre="Réglages" sous={`Propres à ${session.personne?.prenom ?? "toi"}, mémorisés sur cet appareil.`} />
      <div className="rounded-lg border px-4 py-1">
        <Reglage titre={`Palette de ${session.personne?.prenom ?? "la personne"}`} sous="Couleur des actions, de tes messages et de ton avatar">
          <div className="flex basis-full flex-wrap gap-2" role="group" aria-label="Palette">
            {PALETTES.map((p) => (
              <button key={p.cle} className="pal" aria-pressed={prefs.palette === p.cle} onClick={() => modifierPreferences({ palette: p.cle })}>
                <span className="pastille" style={{ background: sombre ? p.sombre : p.clair }} />
                {p.nom}
              </button>
            ))}
          </div>
        </Reglage>
        <Reglage titre="Thème" sous="Le mode système suit le réglage du téléphone">
          <div className="seg" role="group" aria-label="Thème">
            {(
              [
                ["system", "Système"],
                ["light", "Clair"],
                ["dark", "Sombre"],
              ] as [CleTheme, string][]
            ).map(([t, l]) => (
              <button key={t} aria-pressed={prefs.theme === t} onClick={() => modifierPreferences({ theme: t })}>
                {l}
              </button>
            ))}
          </div>
        </Reglage>
        <Reglage titre="Voix" sous="Lecture des briefs et des réponses à voix haute">
          <div className="flex flex-[1_1_280px] flex-wrap items-center justify-end gap-2">
            {voix.length > 0 && (
              <select
                className="liste-deroulante petit w-auto max-w-[240px]"
                value={prefs.voixNavigateur}
                onChange={(e) => modifierPreferences({ voixNavigateur: e.target.value })}
                aria-label="Voix de repli du navigateur"
              >
                <option value="">Voix du foyer</option>
                {voix.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
            )}
            <div className="seg" role="group" aria-label="Vitesse de lecture">
              {(
                [
                  [0.85, "Lente"],
                  [1, "Normale"],
                  [1.2, "Rapide"],
                ] as [number, string][]
              ).map(([r, l]) => (
                <button key={r} aria-pressed={prefs.vitesseVoix === r} onClick={() => modifierPreferences({ vitesseVoix: r })}>
                  {l}
                </button>
              ))}
            </div>
            <BoutonEcouter texte={`Bonjour ${session.personne?.prenom ?? ""}, je suis ${assistant}.`} cle="test-voix" />
          </div>
        </Reglage>
        <Reglage titre="Mode voix : réponses lues à voix haute" sous={`En mode voix, ${assistant} lit ses réponses ; tu peux l’interrompre.`}>
          <Switch checked={prefs.vocaliserLive} onCheckedChange={(v) => modifierPreferences({ vocaliserLive: v })} aria-label="Réponses lues à voix haute en mode voix" />
        </Reglage>
        <Notifications />
        <Reglage titre="Assistant" sous="Son prénom est choisi par le foyer, dans la configuration">
          <span className="text-sm font-medium">{assistant}</span>
        </Reglage>
      </div>

      <section className="section">
        <div className="section-t">
          <h2>Appareils et passkeys</h2>
          {passkeysDisponibles() && (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                inscrire("")
                  .then(async () => {
                    toast("Passkey ajoutée.");
                    await rafraichirSession();
                    return chargerAppareils();
                  })
                  .catch((e) => toast(messageErreur(e)))
              }
            >
              <Fingerprint />
              Ajouter une passkey
            </Button>
          )}
        </div>
        <div className="liste rounded-lg border px-4">
          {appareils?.sessions.map((s) => (
            <div className="ligne" key={`s${s.id}`}>
              <Smartphone className="size-[18px] flex-none text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="ligne-t">
                  {s.appareil}
                  {s.courante && <span className="text-muted-foreground"> · cet appareil</span>}
                </div>
                <div className="ligne-s">{detailSession(s, quand(s.vu))}</div>
              </div>
              {!s.courante && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() =>
                    api("DELETE", `/api/appareils/sessions/${s.id}`)
                      .then(() => {
                        toast("Appareil déconnecté.");
                        return chargerAppareils();
                      })
                      .catch((e) => toast(messageErreur(e)))
                  }
                >
                  Déconnecter
                </Button>
              )}
            </div>
          ))}
          {appareils?.passkeys.map((p) => (
            <div className="ligne" key={`p${p.id}`}>
              <Fingerprint className="size-[18px] flex-none text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <div className="ligne-t">Passkey · {p.nom}</div>
                <div className="ligne-s">Utilisée {quand(p.utilise)}</div>
              </div>
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label={`Supprimer la passkey ${p.nom}`}
                onClick={() =>
                  api("DELETE", `/api/appareils/passkeys/${p.id}`)
                    .then(() => {
                      toast("Passkey supprimée.");
                      return chargerAppareils();
                    })
                    .catch((e) => toast(messageErreur(e)))
                }
              >
                <Trash2 />
              </Button>
            </div>
          ))}
        </div>
        {appareils?.tailscale_attendu && (
          <p className="mt-2 text-sm text-muted-foreground">Connexion acceptée depuis le compte Tailscale {appareils.tailscale_attendu} seulement.</p>
        )}
      </section>

      <section className="section">
        <div className="section-t">
          <h2>Routines</h2>
          <Button variant="ghost" size="sm" onClick={() => naviguer("routines")}>
            Tout voir
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">Horaires des briefs et des routines des capacités, à régler ici ou en parlant.</p>
      </section>

      {session.personne?.role === "proprietaire" && <Etat />}

      <div className="section">
        <Button variant="outline" onClick={onDeconnexion}>
          <LogOut />
          Se déconnecter de cet appareil
        </Button>
      </div>
    </>
  );
}

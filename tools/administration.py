"""Administration par la conversation (lot 7a) : bibliothèque de la capacité `administration` et de `/admin`.

Seuls les propriétaires du foyer (`role: proprietaire` dans `personnes[]` de config.yaml, valeur par défaut d'une
personne listée) administrent. Il s'agit de **configuration** : aucun code n'est écrit ici (lot 7b « atelier »).

- `etat()` : état lisible du système (capacités, crons, validation, dépense du mois, sauvegardes, disque…) ;
- `preparer_*()` : vérifie une demande et renvoie (résumé, payload) pour une action à valider par bouton ;
- `executer(kind, payload)` : applique une action validée (`admin_<action>`), via `tools.config_edition` pour
  config.yaml (validation, sauvegarde, journal) et `tools.capacites` pour l'état des capacités ;
- `definir_parametre()` : réglage déclaré d'une capacité, journalisé et annulable ;
- `journal(n)`, `preparer_annulation(id)` : historique et retour arrière (valeur précédente restaurée).

Les timers systemd suivent tout seuls : `homeos-appliquer-config.path` surveille config.yaml et
`data/capacites/etat.json` et lance `scripts/appliquer-config.sh` (root) ; le bot relit config.yaml à chaque message.
"""

from __future__ import annotations

import json
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from tools import capacites, config_edition, horaires
from tools.common import data_dir, etat_dir, get_logger, iso_now, load_config, now, root_dir

log = get_logger("homeos.administration")

NOM_CAPACITE = "administration"
PREFIXE = "admin"
MESSAGE_RESERVE = "Ce réglage est réservé aux propriétaires du foyer."
MESSAGE_INCONNU = "Je ne sais pas qui me parle : ce réglage est réservé aux propriétaires du foyer."
NOMS_CRONS_FOYER = {"brief_jour": "brief du matin", "brief_hebdo": "brief de la semaine"}
MODES = {
    "humaine": "humaine",
    "manuelle": "humaine",
    "bouton": "humaine",
    "validation": "humaine",
    "avec validation": "humaine",
    "automatique": "automatique",
    "auto": "automatique",
    "sans validation": "automatique",
}
# Kind des actions qu'on peut valider même en pause (sinon /reprendre par la conversation serait impossible).
ACTIONS_HORS_PAUSE = frozenset({"admin_reprendre"})


class ErreurAdministration(capacites.ErreurCapacite):
    """Demande refusée ou impossible (message en français destiné à la personne)."""


# --- qui parle ? ----------------------------------------------------------------------


def _slug(texte: str) -> str:
    t = unicodedata.normalize("NFKD", str(texte).casefold())
    return "".join(c for c in t if not unicodedata.combining(c) and (c.isalnum() or c == "_"))


def appelant(personne: str | None, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Entrée de `personnes[]` (prénom, linux_user, rôle) de la personne courante (slug de son linux_user)."""
    if not personne:
        return None
    config = config if config is not None else load_config()
    from bot.config import normaliser_role

    for p in config.get("personnes") or []:
        if isinstance(p, dict) and _slug(str(p.get("linux_user") or "")) == _slug(personne):
            return {
                "prenom": str(p.get("prenom") or p.get("linux_user")),
                "linux_user": str(p.get("linux_user")),
                "role": normaliser_role(p.get("role")),
            }
    return None


def verifier_proprietaire(personne: str | None, config: dict[str, Any] | None = None) -> str:
    """Prénom de l'appelant s'il est propriétaire ; lève ActionRefusee sinon."""
    qui = appelant(personne, config)
    if qui is None:
        raise capacites.ActionRefusee(MESSAGE_INCONNU)
    if qui["role"] != "proprietaire":
        raise capacites.ActionRefusee(MESSAGE_RESERVE)
    return str(qui["prenom"])


# --- capacités et crons ------------------------------------------------------------------


def trouver_capacite(nom: str, racine: Path | None = None) -> capacites.Capacite:
    """Capacité par nom technique, par titre ou par ancien nom (« Kitchen Daily », « transports », « bus »)."""
    cible = _slug(str(nom).replace(" ", "_"))
    cible_sans = cible.replace("_", "")
    for cap in capacites.decouvrir(racine):
        noms = {cap.nom, _slug(cap.titre.replace(" ", "_")), cap.prefixe}
        noms |= set(cap.manifeste.remplace) if cap.manifeste is not None else set()  # « coupe le bus » → transports
        if cible in noms or cible_sans in {n.replace("_", "") for n in noms}:
            return cap
    connues = ", ".join(c.titre for c in capacites.decouvrir(racine)) or "aucune"
    raise ErreurAdministration(f"Je ne connais pas « {nom} » (capacités : {connues}).")


def _cron_lisible(cap_titre: str | None, cron: str) -> str:
    if cap_titre is None:
        return NOMS_CRONS_FOYER.get(cron, cron.replace("_", " "))
    return f"{cron.replace('_', ' ')} {cap_titre}"


def _mode_validation(config: dict[str, Any], nom: str) -> str:
    bloc = ((config.get("capacites") or {}).get(nom) or {}) if isinstance(config.get("capacites"), dict) else {}
    mode = str(bloc.get("validation", "") if isinstance(bloc, dict) else "").lower()
    return "automatique" if mode in ("automatique", "auto") else "humaine"


# --- état -------------------------------------------------------------------------------------


def _lire_texte(chemin: Path) -> str | None:
    try:
        return chemin.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _lire_json(chemin: Path) -> dict[str, Any] | None:
    texte = _lire_texte(chemin)
    if not texte:
        return None
    try:
        obj = json.loads(texte)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _mesures_mois(donnees: Path) -> dict[str, Any] | None:
    try:
        from bot.db import Db
        from tools import db

        db.connect().close()
        debut = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return Db(donnees / "homeos.sqlite").mesures_mois(debut.isoformat(timespec="seconds"))
    except Exception:
        log.warning("mesures du mois indisponibles", exc_info=True)
        return None


def _disque(donnees: Path) -> dict[str, Any] | None:
    try:
        u = shutil.disk_usage(donnees)
    except OSError:
        return None
    return {"utilise_pct": round(u.used / u.total * 100) if u.total else 0, "libre_go": round(u.free / 1e9, 1)}


def _application(donnees: Path) -> dict[str, Any]:
    """Dernière application des timers par scripts/appliquer-config.sh, et si elle est à jour."""
    info = _lire_json(config_edition.dossier_administration(donnees) / "application.json") or {}
    sources = [donnees / "config.yaml", capacites.chemin_etat(donnees)]
    derniere_modif = max((s.stat().st_mtime for s in sources if s.exists()), default=0.0)
    a_jour = None
    if info.get("quand"):
        try:
            a_jour = datetime.fromisoformat(str(info["quand"])).timestamp() >= derniere_modif - 1
        except ValueError:
            a_jour = None
    return {"derniere": info.get("quand"), "ok": info.get("ok"), "message": info.get("message"), "a_jour": a_jour}


def etat(racine: Path | None = None, donnees: Path | None = None) -> dict[str, Any]:
    """État du système en données simples (outil `admin_etat`, commande `/admin`)."""
    donnees = Path(donnees) if donnees is not None else data_dir()
    config = load_config()
    etat_caps = capacites.lire_etat(donnees)
    liste: list[dict[str, Any]] = []
    for cap in capacites.decouvrir(racine):
        infos: dict[str, Any] = {
            "nom": cap.nom,
            "titre": cap.titre,
            "description": cap.manifeste.description if cap.manifeste else "",
            "statut": capacites.statut_effectif(cap, etat_caps),
            "validation": _mode_validation(config, cap.nom),
            "derniere_verification": (etat_caps.get(cap.nom) or {}).get("smoke"),
            "crons": [],
        }
        if cap.manifeste is not None:
            for c in cap.manifeste.crons:
                quand = capacites.quand_cron(cap, c, config)
                infos["crons"].append({"cron": c.nom, "quand": quand, "lisible": horaires.lisible(quand)})
        if cap.erreurs:
            infos["erreurs"] = list(cap.erreurs)
        liste.append(infos)
    crons_cfg = config.get("crons") if isinstance(config.get("crons"), dict) else {}
    crons_foyer = []
    for nom, defaut in config_edition.CRONS_FOYER.items():
        quand = str(crons_cfg.get(nom, defaut))
        crons_foyer.append({"cron": nom, "nom": NOMS_CRONS_FOYER[nom], "quand": quand, "lisible": horaires.lisible(quand)})
    kd = next((c for c in liste if c["nom"] == "kitchen_daily"), None)
    session_kd = None
    if kd is not None and kd["statut"] in capacites.STATUTS_CHARGES:
        verif = kd.get("derniere_verification") or {}
        session_kd = {"ok": verif.get("ok"), "message": verif.get("message"), "quand": verif.get("quand")}
    restauration = _lire_json(donnees / ".derniere-restauration")
    return {
        "capacites": liste,
        "crons_foyer": crons_foyer,
        "pause": (donnees / "PAUSE").exists(),
        "depense_mois": _mesures_mois(donnees),
        "sauvegarde": {
            "derniere": _lire_texte(donnees / ".derniere-sauvegarde"),
            "copie_hors_site": _lire_texte(donnees / ".derniere-copie-distante"),
            "restauration_testee": (
                {k: restauration.get(k) for k in ("date", "ok", "message")} if restauration else None
            ),
        },
        "session_kitchen_daily": session_kd,
        "disque": _disque(donnees),
        "timers": _application(donnees),
        "derniers_reglages": journal(3, donnees),
    }


def _date_courte(iso: Any) -> str:
    texte = str(iso or "")
    return f"{texte[8:10]}/{texte[5:7]} {texte[11:16]}" if len(texte) >= 16 else (texte or "jamais")


def etat_texte(e: dict[str, Any]) -> str:
    """État en texte brut lisible (une ligne par sujet), pour /admin."""
    statuts = {"actif": "active", "essai": "en essai", "inactif": "désactivée", "invalide": "à corriger"}
    lignes = ["État du système"]
    if e.get("pause"):
        lignes.append("⏸ En pause : rappels et validations suspendus (/reprendre).")
    lignes.append("")
    lignes.append("Capacités :")
    for c in e["capacites"]:
        detail = [statuts.get(c["statut"], c["statut"])]
        if c["validation"] == "automatique":
            detail.append("validation automatique")
        for cron in c["crons"]:
            detail.append(f"{cron['cron'].replace('_', ' ')} {cron['lisible']}")
        verif = c.get("derniere_verification") or {}
        if verif and c["statut"] in capacites.STATUTS_CHARGES:
            detail.append("vérifiée " + ("OK" if verif.get("ok") else f"en échec ({verif.get('message', '')})"))
        lignes.append(f"• {c['titre']} : {', '.join(detail)}")
    lignes.append("")
    lignes.append("Rendez-vous programmés : " + ", ".join(f"{c['nom']} {c['lisible']}" for c in e["crons_foyer"]))
    d = e.get("depense_mois")
    if d:
        latence = f"{d['latence_mediane_ms'] / 1000:.1f} s" if d.get("latence_mediane_ms") is not None else "—"
        lignes.append(f"Ce mois-ci : {d['cout_usd']:.2f} $ estimés, {d['reponses']} réponses, latence médiane {latence}")
    s = e["sauvegarde"]
    resto = s.get("restauration_testee") or {}
    resto_txt = "jamais testée" if not resto else (
        f"{_date_courte(resto.get('date'))} {'OK' if resto.get('ok') else 'en échec'}"
    )
    lignes.append(
        f"Sauvegarde : {_date_courte(s.get('derniere'))} ; hors site : "
        f"{_date_courte(s.get('copie_hors_site')) if s.get('copie_hors_site') else 'non configurée'} ; "
        f"restauration : {resto_txt}"
    )
    kd = e.get("session_kitchen_daily")
    if kd is not None:
        if kd.get("ok") is None:
            lignes.append("Session Kitchen Daily : pas encore vérifiée")
        else:
            lignes.append("Session Kitchen Daily : " + ("valide" if kd["ok"] else "à refaire (demande-moi comment)"))
    if e.get("disque"):
        lignes.append(f"Disque : {e['disque']['utilise_pct']} % utilisé, {e['disque']['libre_go']} Go libres")
    t = e.get("timers") or {}
    if t.get("a_jour") is False:
        lignes.append("Horaires : changement en cours d'application sur le serveur")
    elif t.get("ok") is False:
        lignes.append(f"Horaires : dernière application en échec ({t.get('message') or 'voir le serveur'})")
    if e.get("derniers_reglages"):
        lignes.append("")
        lignes.append("Derniers réglages :")
        for r in e["derniers_reglages"]:
            lignes.append(f"• n°{r['id']} {_date_courte(r['quand'])} {r['auteur']} : {r['resume']}"
                          + (" (annulé)" if r.get("annule_par") else ""))
    return "\n".join(lignes)


# --- journal ---------------------------------------------------------------------------------


def journal(n: int = 10, donnees: Path | None = None) -> list[dict[str, Any]]:
    """Les n dernières actions d'administration, de la plus récente à la plus ancienne."""
    entrees = config_edition.lire_journal(donnees)
    annulees = {e["annule"]: e["id"] for e in entrees if isinstance(e.get("annule"), int)}
    n = max(1, min(int(n), 50))
    resultat = []
    for e in reversed(entrees[-n:]):
        resultat.append(
            {
                "id": e["id"],
                "quand": e.get("quand"),
                "auteur": e.get("auteur"),
                "resume": e.get("resume"),
                "avant": e.get("avant"),
                "apres": e.get("apres"),
                **({"annule": e["annule"]} if e.get("annule") else {}),
                **({"annule_par": annulees[e["id"]]} if e["id"] in annulees else {}),
            }
        )
    return resultat


# --- préparations (actions à valider) -------------------------------------------------------------


def _payload(kind: str, auteur: str, resume: str, **donnees: Any) -> tuple[str, dict[str, Any]]:
    return resume, {"action": kind, "auteur": auteur, "resume": resume, **donnees}


def preparer_activation(nom: str, activer: bool, auteur: str, racine: Path | None = None,
                        donnees: Path | None = None) -> tuple[str, dict[str, Any]]:
    cap = trouver_capacite(nom, racine)
    statut = capacites.statut_effectif(cap, capacites.lire_etat(donnees))
    if activer:
        if not cap.valide:
            raise ErreurAdministration(f"« {cap.titre} » ne peut pas être activée : {'; '.join(cap.erreurs)}")
        if statut == "actif":
            raise ErreurAdministration(f"« {cap.titre} » est déjà active.")
        resume = f"Activer « {cap.titre} »"
        if cap.manifeste is not None and cap.manifeste.origine == "assistant":
            resume += " (écrite par l'assistant : l'activer vaut validation humaine)"
    else:
        if cap.nom == NOM_CAPACITE:
            raise ErreurAdministration(
                "Je ne désactive pas l'administration elle-même : on ne pourrait plus la réactiver en me parlant."
            )
        if statut == "inactif":
            raise ErreurAdministration(f"« {cap.titre} » est déjà désactivée.")
        resume = f"Désactiver « {cap.titre} » (ses données sont conservées)"
    kind = "admin_capacite_activer" if activer else "admin_capacite_desactiver"
    return _payload(kind, auteur, resume, capacite=cap.nom)


def preparer_retrait(nom: str, auteur: str, racine: Path | None = None) -> tuple[str, dict[str, Any]]:
    """Lot 7b : retirer une capacité fabriquée par l'atelier (code archivé, données conservées, non annulable)."""
    cap = trouver_capacite(nom, racine)
    if not capacites.de_l_atelier(cap):
        raise ErreurAdministration(
            f"« {cap.titre} » fait partie du système : je peux la désactiver, pas la retirer."
        )
    resume = f"Retirer « {cap.titre} » (fabriquée par l'atelier ; ses données sont conservées)"
    return _payload("admin_capacite_retirer", auteur, resume, capacite=cap.nom)


def preparer_validation(nom: str, mode: str, auteur: str, racine: Path | None = None) -> tuple[str, dict[str, Any]]:
    cap = trouver_capacite(nom, racine)
    canon = MODES.get(str(mode or "").strip().lower())
    if canon is None:
        raise ErreurAdministration("Mode inconnu : « humaine » (je propose, vous validez) ou « automatique ».")
    config = load_config()
    if _mode_validation(config, cap.nom) == canon:
        raise ErreurAdministration(f"« {cap.titre} » est déjà en validation {canon}.")
    if canon == "automatique":
        resume = f"« {cap.titre} » en validation automatique (j'agis sans bouton Valider, puis je préviens)"
        if cap.nom == NOM_CAPACITE:
            resume += " — déconseillé pour l'administration"
    else:
        resume = f"« {cap.titre} » en validation humaine (bouton Valider avant chaque action)"
    return _payload("admin_validation", auteur, resume, cle=f"capacites.{cap.nom}.validation", valeur=canon)


def preparer_cron(nom: str, cron: str, quand: str, auteur: str, racine: Path | None = None) -> tuple[str, dict[str, Any]]:
    try:
        interne = horaires.analyser(quand)
        capacites.oncalendar(interne)
    except ValueError as exc:
        raise ErreurAdministration(str(exc)) from None
    config = load_config()
    nom_n = _slug(str(nom).replace(" ", "_"))
    cron_n = _slug(str(cron or "").replace(" ", "_"))
    if nom_n in config_edition.CRONS_FOYER or (nom_n in ("brief", "briefs") and cron_n in config_edition.CRONS_FOYER):
        cle_cron = nom_n if nom_n in config_edition.CRONS_FOYER else cron_n
        cle = f"crons.{cle_cron}"
        avant = str(((config.get("crons") or {}) if isinstance(config.get("crons"), dict) else {}).get(
            cle_cron, config_edition.CRONS_FOYER[cle_cron]))
        libelle = NOMS_CRONS_FOYER[cle_cron]
    else:
        cap = trouver_capacite(nom, racine)
        if cap.manifeste is None:
            raise ErreurAdministration(f"« {cap.titre} » est invalide : {'; '.join(cap.erreurs)}")
        declares = {c.nom: c for c in cap.manifeste.crons}
        if not declares:
            raise ErreurAdministration(f"« {cap.titre} » n'a pas de rendez-vous programmé.")
        if not cron_n and len(declares) == 1:
            cron_n = next(iter(declares))
        if cron_n not in declares:
            raise ErreurAdministration(
                f"« {cap.titre} » a ces rendez-vous programmés : {', '.join(declares)}. Lequel changer ?"
            )
        cle = f"capacites.{cap.nom}.crons.{cron_n}"
        avant = capacites.quand_cron(cap, declares[cron_n], config)
        libelle = _cron_lisible(cap.titre, cron_n)
    if horaires.normaliser_interne(avant) == interne:
        raise ErreurAdministration(f"Le {libelle} est déjà {horaires.lisible(interne)}.")
    resume = f"{libelle[0].upper()}{libelle[1:]} : {horaires.lisible(interne)} (au lieu de {horaires.lisible(avant)})"
    return _payload("admin_cron", auteur, resume, cle=cle, valeur=interne, libelle=libelle,
                    avant_lisible=horaires.lisible(avant))


def preparer_pause(pause: bool, auteur: str, donnees: Path | None = None) -> tuple[str, dict[str, Any]]:
    en_pause = ((Path(donnees) if donnees is not None else data_dir()) / "PAUSE").exists()
    if pause and en_pause:
        raise ErreurAdministration("Le foyer est déjà en pause.")
    if not pause and not en_pause:
        raise ErreurAdministration("Le foyer n'est pas en pause.")
    if pause:
        return _payload("admin_pause", auteur, "Mettre en pause rappels, briefs et validations")
    return _payload("admin_reprendre", auteur, "Reprendre rappels, briefs et validations")


def _entree(identifiant: int, donnees: Path | None) -> dict[str, Any]:
    entrees = config_edition.lire_journal(donnees)
    entree = next((e for e in entrees if e["id"] == int(identifiant)), None)
    if entree is None:
        raise ErreurAdministration(f"Réglage n°{identifiant} introuvable dans le journal.")
    deja = next((e for e in entrees if e.get("annule") == entree["id"]), None)
    if deja is not None:
        raise ErreurAdministration(f"Le réglage n°{identifiant} a déjà été annulé (n°{deja['id']}).")
    return entree


def _valeur_actuelle(entree: dict[str, Any], donnees: Path | None, racine: Path | None) -> Any:
    genre = entree.get("type")
    if genre == "config":
        return config_edition.valeur(config_edition.lire_brut(donnees), entree["cle"])
    if genre == "capacite":
        cap = capacites.trouver(entree["capacite"], racine)
        return capacites.statut_effectif(cap, capacites.lire_etat(donnees))
    if genre == "parametre":
        return capacites.lire_parametres(entree["capacite"], donnees).get(entree["cle"])
    if genre == "pause":
        return ((Path(donnees) if donnees is not None else data_dir()) / "PAUSE").exists()
    raise ErreurAdministration(f"Le réglage n°{entree['id']} ne peut pas être annulé.")


def preparer_annulation(identifiant: int, auteur: str, racine: Path | None = None,
                        donnees: Path | None = None) -> tuple[str, dict[str, Any]]:
    entree = _entree(identifiant, donnees)
    actuelle = _valeur_actuelle(entree, donnees, racine)
    if actuelle != entree.get("apres"):
        raise ErreurAdministration(
            f"Le réglage n°{entree['id']} a changé depuis (valeur actuelle : {actuelle!r}) : "
            "annule d'abord le changement le plus récent."
        )
    resume = f"Annuler le réglage n°{entree['id']} ({entree.get('resume')}) : retour à la valeur précédente"
    return _payload("admin_annuler", auteur, resume, journal_id=entree["id"])


# --- exécution (après Valider) ---------------------------------------------------------------------


def _annuler_conseil(entree: dict[str, Any]) -> str:
    return f" Tu peux annuler en me le disant (réglage n°{entree['id']})."


def _appliquer_capacite(nom: str, activer: bool, auteur: str, racine: Path | None, donnees: Path | None,
                        annule: int | None = None, etat_cible: dict[str, Any] | None = None) -> dict[str, Any]:
    cap = capacites.trouver(nom, racine)
    with config_edition.verrou(donnees):
        avant_etat = dict(capacites.lire_etat(donnees).get(cap.nom) or {})
        avant = capacites.statut_effectif(cap, capacites.lire_etat(donnees))
        if etat_cible is not None or annule is not None:
            capacites.remplacer_etat(cap.nom, etat_cible, donnees)
        elif activer:
            capacites.activer(cap.nom, racine, donnees, par=auteur)
        else:
            capacites.desactiver(cap.nom, racine, donnees)
        apres = capacites.statut_effectif(cap, capacites.lire_etat(donnees))
        verbe = "activée" if apres == "actif" else ("désactivée" if apres == "inactif" else f"en {apres}")
        entree = {
            "type": "capacite",
            "auteur": auteur,
            "capacite": cap.nom,
            "avant": avant,
            "apres": apres,
            "etat_avant": avant_etat,
            "resume": f"« {cap.titre} » {verbe}",
        }
        if annule is not None:
            entree["annule"] = annule
        return config_edition.journaliser(entree, donnees)


def _appliquer_pause(pause: bool, auteur: str, donnees: Path | None, annule: int | None = None) -> dict[str, Any]:
    chemin = (Path(donnees) if donnees is not None else data_dir()) / "PAUSE"
    with config_edition.verrou(donnees):
        avant = chemin.exists()
        if pause:
            chemin.write_text(f"pause demandée par {auteur} le {iso_now()}\n", encoding="utf-8")
        else:
            chemin.unlink(missing_ok=True)
        entree = {
            "type": "pause",
            "auteur": auteur,
            "avant": avant,
            "apres": pause,
            "resume": "Foyer mis en pause" if pause else "Fin de la pause",
        }
        if annule is not None:
            entree["annule"] = annule
        return config_edition.journaliser(entree, donnees)


def _appliquer_parametre(nom: str, cle: str, nouvelle: Any, auteur: str, racine: Path | None,
                         donnees: Path | None, annule: int | None = None) -> dict[str, Any]:
    cap = capacites.trouver(nom, racine)
    with config_edition.verrou(donnees):
        avant = capacites.lire_parametres(cap.nom, donnees).get(cle)
        if nouvelle is None:
            capacites.retirer_parametre(cap.nom, cle, donnees)
        else:
            capacites.definir_parametre(cap.nom, cle, nouvelle, racine, donnees)
        apres = capacites.lire_parametres(cap.nom, donnees).get(cle)
        entree = {
            "type": "parametre",
            "auteur": auteur,
            "capacite": cap.nom,
            "cle": cle,
            "avant": avant,
            "apres": apres,
            "resume": f"« {cap.titre} », réglage {cle} : {apres if apres is not None else 'effacé'}",
        }
        if annule is not None:
            entree["annule"] = annule
        return config_edition.journaliser(entree, donnees)


def definir_parametre(nom: str, cle: str, valeur: Any, auteur: str, racine: Path | None = None,
                      donnees: Path | None = None) -> dict[str, Any]:
    """Réglage déclaré d'une capacité (= `capacites_parametrer`), journalisé et annulable."""
    cap = trouver_capacite(nom, racine)
    entree = _appliquer_parametre(cap.nom, cle, valeur, auteur, racine, donnees)
    return {
        "message": f"Fait. {entree['resume']} (avant : {entree['avant'] if entree['avant'] is not None else 'vide'})."
        + _annuler_conseil(entree),
        "parametres": capacites.statut_detaille(cap.nom, racine, donnees)["parametres"],
    }


def _annuler(payload: dict[str, Any], auteur: str, racine: Path | None, donnees: Path | None) -> str:
    entree = _entree(int(payload["journal_id"]), donnees)
    actuelle = _valeur_actuelle(entree, donnees, racine)
    if actuelle != entree.get("apres"):
        raise ErreurAdministration(f"Le réglage n°{entree['id']} a changé entre-temps : rien n'est annulé.")
    genre = entree["type"]
    if genre == "config":
        nouvelle = config_edition.modifier(
            entree["cle"], entree.get("avant"), auteur, f"Annulation du n°{entree['id']} : {entree.get('resume')}",
            donnees, annule=entree["id"],
        )
    elif genre == "capacite":
        nouvelle = _appliquer_capacite(entree["capacite"], False, auteur, racine, donnees, annule=entree["id"],
                                       etat_cible=entree.get("etat_avant") or None)
    elif genre == "parametre":
        nouvelle = _appliquer_parametre(entree["capacite"], entree["cle"], entree.get("avant"), auteur, racine,
                                        donnees, annule=entree["id"])
    else:
        nouvelle = _appliquer_pause(bool(entree.get("avant")), auteur, donnees, annule=entree["id"])
    return f"Fait. Réglage n°{entree['id']} annulé : {entree.get('resume')} → retour à la valeur précédente (n°{nouvelle['id']})."


def executer(kind: str, payload: dict[str, Any], racine: Path | None = None, donnees: Path | None = None) -> str:
    """Applique une action d'administration validée ; renvoie le message pour le foyer."""
    auteur = str(payload.get("auteur") or "?")
    if kind == "admin_cron":
        entree = config_edition.modifier(payload["cle"], payload["valeur"], auteur, payload.get("resume", ""), donnees)
        libelle = str(payload.get("libelle") or "rendez-vous")
        return (
            f"Fait. Le {libelle} est maintenant {horaires.lisible(entree['apres'])} "
            f"(avant : {horaires.lisible(entree['avant']) if entree['avant'] else payload.get('avant_lisible', '?')})."
            + _annuler_conseil(entree)
        )
    if kind == "admin_validation":
        entree = config_edition.modifier(payload["cle"], payload["valeur"], auteur, payload.get("resume", ""), donnees)
        return f"Fait. {payload.get('resume', '')}." + _annuler_conseil(entree)
    if kind in ("admin_capacite_activer", "admin_capacite_desactiver"):
        entree = _appliquer_capacite(payload["capacite"], kind == "admin_capacite_activer", auteur, racine, donnees)
        return f"Fait. {entree['resume']}." + _annuler_conseil(entree)
    if kind == "admin_capacite_retirer":
        with config_edition.verrou(donnees):
            retrait = capacites.retirer(payload["capacite"], auteur, donnees)
            config_edition.journaliser({
                "type": "retrait", "auteur": auteur, "capacite": retrait["nom"], "avant": "installée",
                "apres": "retirée", "resume": f"« {retrait['titre']} » retirée", "archive": retrait["archive"],
            }, donnees)
        return (f"Fait. « {retrait['titre']} » est retirée (ses données sont conservées). "
                "Pour la retrouver, il faudra la redemander à l'atelier.")
    if kind in ("admin_pause", "admin_reprendre"):
        entree = _appliquer_pause(kind == "admin_pause", auteur, donnees)
        suite = " /reprendre ou « reprends » pour réactiver." if kind == "admin_pause" else ""
        return f"Fait. {entree['resume']}.{suite}"
    if kind == "admin_annuler":
        return _annuler(payload, auteur, racine, donnees)
    raise ErreurAdministration(f"Action d'administration inconnue : « {kind} ».")


# --- reconnexion ---------------------------------------------------------------------------------------


def reconnexion(nom: str, racine: Path | None = None, donnees: Path | None = None) -> str:
    """Procédure de reconnexion d'une capacité (jamais d'identifiant dans la conversation)."""
    cap = trouver_capacite(nom, racine)
    verif = (capacites.lire_etat(donnees).get(cap.nom) or {}).get("smoke") or {}
    etat_txt = ""
    if verif:
        etat_txt = (f" Dernière vérification ({_date_courte(verif.get('quand'))}) : "
                    + ("tout va bien." if verif.get("ok") else f"{verif.get('message', 'en échec')}."))
    if cap.nom == "kitchen_daily":
        commande = (
            f"cd {root_dir()} && sudo -u homeos HOMEOS_DATA={data_dir()} HOMEOS_ETAT={etat_dir()} "
            ".venv/bin/python -m tools.kitchen_daily login"
        )
        return (
            "La connexion au site Kitchen Daily se refait sur le serveur, par la personne qui l'administre "
            f"(une minute, identifiants tapés dans le terminal, jamais ici) : {commande}.{etat_txt} "
            "Bientôt : directement depuis la carte de l'appli."
        )
    if cap.manifeste is not None and cap.manifeste.secrets:
        return (
            f"Les identifiants de « {cap.titre} » se saisissent hors de la conversation : /identifiants {cap.nom} "
            f"donne la marche à suivre.{etat_txt}"
        )
    return f"« {cap.titre} » n'a pas de connexion à refaire.{etat_txt}"

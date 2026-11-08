#!/usr/bin/env python3
"""Questionnaire d'installation d'un nouveau foyer (lot 6) — appelé par install/installer-un-foyer.sh.

Pose, en français et une question à la fois, tout ce qui dépend du foyer : prénom de l'assistant, adultes
(prénoms, rôles, compte Telegram, compte Claude), enfants et animaux (sans donnée sensible), commune, horaires
des briefs, capacités, Telegram, clé API, agendas, Tailscale, Vaultwarden, sauvegarde hors site. Rien n'est
supposé : aucune valeur par défaut ne vient d'un foyer réel.

Écrit dans le dossier `--sortie` (700, root) :
  config.yaml   configuration complète (copiée ensuite dans /srv/home-os/data/config.yaml)
  secrets.env   valeurs secrètes saisies (token Telegram, clé API, URLs iCal) — 600, effacé après usage
  identite.md   fiche « Identité du foyer » pré-remplie (relue par l'assistant, complétée à la séance 1)
  choix.env     options d'installation lues par le script shell (aucun secret sauf la clé Tailscale)

Usage :
  python3 install/configurer.py --sortie DOSSIER                   # interactif
  python3 install/configurer.py --sortie DOSSIER --reponses R.json # sans question (tests, réinstallation)
Dépendances : bibliothèque standard seulement (Ubuntu vierge, PyYAML pas encore installé).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

NOM_ASSISTANT_DEFAUT = "Firmin"  # même valeur que tools.common.NOM_ASSISTANT_DEFAUT (pas d'import : venv absent)
COMPTES_RESERVES = {
    "root", "admin", "homeos", "homeos-web", "ubuntu", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "nobody", "systemd-network", "syslog", "sshd",
    "foyer", "tinyproxy", "messagebus", "tailscale", "caddy",
}
ROLES = {"proprietaire": "propriétaire (règle le système en conversation)", "membre": "membre (utilise, ne règle pas)"}
JOURS = {"lun": "Mon", "mar": "Tue", "mer": "Wed", "jeu": "Thu", "ven": "Fri", "sam": "Sat", "dim": "Sun"}
RE_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
RE_HEURE = re.compile(r"^([01]?\d|2[0-3])[:hH]([0-5]\d)?$")


class Abandon(Exception):
    """L'utilisateur a interrompu le questionnaire."""


# --- validations pures (testées dans tests/test_installateur.py) ------------------------------------------------


def slug(texte: str) -> str:
    """« Élodie » → « elodie », « Jean Paul » → « jean-paul » : identifiant Linux d'une personne."""
    t = unicodedata.normalize("NFKD", texte.strip().casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if t and not t[0].isalpha():
        t = "p-" + t
    return t[:32]


def identifiant_valide(ident: str) -> str | None:
    """Message d'erreur si l'identifiant Linux n'est pas utilisable, sinon None."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", ident or ""):
        return "lettres minuscules sans accent, chiffres et tirets, en commençant par une lettre"
    if ident in COMPTES_RESERVES:
        return f"« {ident} » est réservé par le système, choisis-en un autre"
    return None


def heure(texte: str) -> str:
    """« 7h », « 7h30 », « 07:15 » → « 07:15 » ; ValueError sinon."""
    m = RE_HEURE.match(texte.strip())
    if not m:
        raise ValueError("heure attendue, par exemple 7h15")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def jour_heure(texte: str) -> str:
    """« dim 19h » → « Sun 19:00 » (format des crons de config.yaml)."""
    morceaux = texte.strip().lower().split()
    if len(morceaux) != 2 or morceaux[0][:3] not in JOURS:
        raise ValueError("jour et heure attendus, par exemple « dim 19h »")
    return f"{JOURS[morceaux[0][:3]]} {heure(morceaux[1])}"


def telegram_id(texte: str, groupe: bool = False) -> int:
    t = texte.strip().replace(" ", "")
    if not re.fullmatch(r"-?\d{3,20}", t):
        raise ValueError("nombre attendu (donné par @userinfobot)")
    valeur = int(t)
    if groupe and valeur >= 0:
        raise ValueError("l'identifiant d'un groupe est négatif (il commence par -100)")
    if not groupe and valeur <= 0:
        raise ValueError("l'identifiant d'une personne est un nombre positif")
    return valeur


def token_telegram(texte: str) -> str:
    t = texte.strip()
    if not RE_TOKEN.match(t):
        raise ValueError("format attendu : 123456789:AAE… (copié depuis @BotFather)")
    return t


def prenom_assistant(texte: str) -> str:
    t = texte.strip()
    if not re.fullmatch(r"[^\W\d_][\w' -]{0,29}", t):
        raise ValueError("un prénom de 30 caractères au plus (lettres, espaces, tirets, apostrophes)")
    return t


def cle_tailscale(texte: str) -> str:
    t = texte.strip()
    if t and not re.fullmatch(r"tskey-[A-Za-z0-9-]{10,}", t):
        raise ValueError("une clé Tailscale commence par tskey-")
    return t


def cle_api(texte: str) -> str:
    t = texte.strip()
    if t and not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,}", t):
        raise ValueError("une clé API Anthropic commence par sk-ant-")
    return t


def url_ical(texte: str) -> str:
    t = texte.strip()
    if t and not re.fullmatch(r"https://[^\s\"']+", t):
        raise ValueError("adresse https://… attendue (Google Agenda → « Adresse secrète au format iCal »)")
    return t


def oui_non(texte: str) -> bool:
    t = texte.strip().lower()
    if t in ("o", "oui", "y", "yes", "1", "vrai"):
        return True
    if t in ("n", "non", "no", "0", "faux"):
        return False
    raise ValueError("réponds oui ou non")


def yaml_texte(valeur: Any) -> str:
    """Scalaire YAML sûr : une chaîne JSON est une chaîne YAML valide entre guillemets doubles."""
    if valeur is None:
        return "null"
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    if isinstance(valeur, int):
        return str(valeur)
    return json.dumps(str(valeur), ensure_ascii=False)


# --- modèle des réponses ------------------------------------------------------------------------------------


@dataclass
class Adulte:
    prenom: str
    linux_user: str
    role: str
    telegram_id: int
    auth: str  # abonnement | cle_api
    ical: str = ""


@dataclass
class Reponses:
    assistant: str = NOM_ASSISTANT_DEFAUT
    tutoiement: bool = True
    commune: str = ""
    adultes: list[Adulte] = field(default_factory=list)
    enfants: list[dict[str, str]] = field(default_factory=list)
    animaux: list[dict[str, str]] = field(default_factory=list)
    telegram_token: str = ""
    groupe_telegram_id: int | None = None
    cle_api: str = ""
    ical_foyer: str = ""
    brief_jour: str = "07:15"
    brief_hebdo: str = "Sun 19:00"
    kitchen_daily: bool = False
    transports: bool = True
    reseaux: str = ""        # réseaux locaux choisis (ex. « cartreize ») ; le train SNCF est toujours disponible
    tailscale: bool = False
    tailscale_cle: str = ""
    vaultwarden: bool = False
    hors_site: bool = False


# Réseaux locaux fournis en standard par la capacité transports (capabilities/transports/fournisseurs/, portée locale).
RESEAUX_LOCAUX = {"cartreize": "cars de la Métropole Aix-Marseille-Provence"}


def reseaux(t: str) -> str:
    """« Cartreize » → « cartreize » ; plusieurs séparés par des virgules ; vide accepté (aucun réseau supposé)."""
    noms = [x.strip().lower() for x in str(t or "").replace(";", ",").split(",") if x.strip()]
    for n in noms:
        if n not in RESEAUX_LOCAUX:
            raise ValueError(f"réseau inconnu « {n} » (connus : {', '.join(RESEAUX_LOCAUX)} ; les autres s'ajoutent en conversation)")
    return ", ".join(dict.fromkeys(noms))


def depuis_dict(d: dict[str, Any]) -> Reponses:
    """Réponses non interactives (`--reponses`), validées comme les réponses saisies."""
    r = Reponses()
    r.assistant = prenom_assistant(str(d.get("assistant") or NOM_ASSISTANT_DEFAUT))
    r.tutoiement = bool(d.get("tutoiement", True))
    r.commune = str(d.get("commune") or "").strip()
    vus: set[str] = set()
    for a in d.get("adultes") or []:
        prenom = str(a["prenom"]).strip()
        ident = str(a.get("identifiant") or slug(prenom))
        if (err := identifiant_valide(ident)) or ident in vus:
            raise ValueError(f"adulte {prenom} : identifiant « {ident} » : {err or 'déjà utilisé'}")
        vus.add(ident)
        role = str(a.get("role") or "proprietaire")
        if role not in ROLES:
            raise ValueError(f"adulte {prenom} : rôle « {role} » inconnu (proprietaire ou membre)")
        auth = str(a.get("claude") or "abonnement")
        if auth not in ("abonnement", "cle_api"):
            raise ValueError(f"adulte {prenom} : compte Claude « {auth} » inconnu (abonnement ou cle_api)")
        r.adultes.append(Adulte(prenom, ident, role, telegram_id(str(a["telegram_id"])), auth, url_ical(str(a.get("ical") or ""))))
    if not r.adultes:
        raise ValueError("au moins un adulte est nécessaire")
    if not any(a.role == "proprietaire" for a in r.adultes):
        raise ValueError("au moins un adulte doit être propriétaire")
    r.enfants = [{"prenom": str(e["prenom"]).strip(), "age": str(e.get("age") or "").strip()} for e in d.get("enfants") or []]
    r.animaux = [{"nom": str(a["nom"]).strip(), "espece": str(a.get("espece") or "").strip()} for a in d.get("animaux") or []]
    r.telegram_token = token_telegram(str(d.get("telegram_token") or "")) if d.get("telegram_token") else ""
    g = d.get("groupe_telegram_id")
    r.groupe_telegram_id = telegram_id(str(g), groupe=True) if g not in (None, "") else None
    r.cle_api = cle_api(str(d.get("cle_api") or ""))
    r.ical_foyer = url_ical(str(d.get("ical_foyer") or ""))
    r.brief_jour = heure(str(d.get("brief_jour") or "07:15"))
    r.brief_hebdo = jour_heure(str(d["brief_hebdo"])) if d.get("brief_hebdo") else "Sun 19:00"
    for cle in ("kitchen_daily", "tailscale", "vaultwarden", "hors_site"):
        setattr(r, cle, bool(d.get(cle, False)))
    # Transports (remplace « bus ») : actifs par défaut, sans supposer de réseau local ; `bus: true` (anciennes réponses)
    # = réseau Cartreize, seul réseau de bus du lot 5c.
    r.transports = bool(d.get("transports", True))
    r.reseaux = reseaux(str(d.get("reseaux") or ("cartreize" if d.get("bus") else "")))
    r.tailscale_cle = cle_tailscale(str(d.get("tailscale_cle") or ""))
    if r.vaultwarden and not r.tailscale:
        raise ValueError("Vaultwarden demande Tailscale (aucun port public ouvert)")
    return r


# --- génération des fichiers ------------------------------------------------------------------------------------


def variable_ical(a: Adulte) -> str:
    return "ICAL_" + a.linux_user.upper().replace("-", "_")


def config_yaml(r: Reponses) -> str:
    l: list[str] = [
        "# Firmin Assistant — configuration du foyer, générée par install/configurer.py le " + date.today().isoformat(),
        "# Modifiable ensuite en parlant à l'assistant (propriétaires) ou ici, puis : sudo systemctl restart homeos-bot",
        "",
        "assistant:",
        f"  nom: {yaml_texte(r.assistant)}",
        f"  tutoiement: {yaml_texte(r.tutoiement)}",
        "  langue: fr",
        "",
        "personnes:                   # allowlist stricte : tout identifiant Telegram absent d'ici est ignoré",
    ]
    for a in r.adultes:
        l += [
            f"  - telegram_id: {a.telegram_id}",
            f"    prenom: {yaml_texte(a.prenom)}",
            f"    linux_user: {a.linux_user}",
            f"    role: {a.role}",
            f"    auth: {a.auth}             # abonnement = son compte Claude ; cle_api = clé API du foyer",
        ]
    l += ["", f"groupe_telegram_id: {yaml_texte(r.groupe_telegram_id)}   # groupe Telegram du foyer (briefs, rappels)", ""]
    l += [
        "crons:                       # heures locales ; aussi réglables en conversation (« mets le brief à 7h30 »)",
        f"  brief_jour: {yaml_texte(r.brief_jour)}",
        f"  brief_hebdo: {yaml_texte(r.brief_hebdo)}",
        "",
    ]
    agendas = [("Foyer", "ICAL_FOYER")] if r.ical_foyer else []
    agendas += [(a.prenom, variable_ical(a)) for a in r.adultes if a.ical]
    if agendas:
        l.append("agendas:                     # agendas iCal (URL secrètes dans /etc/homeos/ical.env, jamais ici)")
        for nom, variable in agendas:
            l += [f"  - nom: {yaml_texte(nom)}", f"    ical_url_env: {variable}"]
    else:
        l.append("agendas: []                  # à ajouter plus tard : README, « Agendas »")
    l += [
        "",
        "plafonds:",
        "  confirmation_requise_eur: 0    # toute dépense demande confirmation",
        "",
        "coffre:",
        "  backend: fichier",
        "",
        "ia:",
        "  modele_simple: haiku",
        "  modele: sonnet",
        "  modele_decisions: opus",
        "  routeur: true",
        "  session_max_reponses: 20",
        "  session_ttl_heures: 2",
    ]
    if r.commune:
        l += ["", "meteo:", f"  commune: {yaml_texte(r.commune)}   # commune du foyer (météo de l'appli), jamais l'adresse"]
    if r.kitchen_daily:
        l += [
            "",
            "kitchen_daily:               # plats livrés : connexion au site à faire une fois (README, Kitchen Daily)",
            '  url: "https://www.kitchen-daily.fr"',
            "  formule_plats: 18          # repli si le nombre de plats n'est pas lisible sur le site",
        ]
    return "\n".join(l) + "\n"


def secrets_env(r: Reponses) -> str:
    lignes = [
        "# Secrets saisis à l'installation : répartis dans /etc/homeos par installer-un-foyer.sh, puis effacés.",
        f"TELEGRAM_BOT_TOKEN={r.telegram_token}",
        f"ANTHROPIC_API_KEY={r.cle_api}",
        f"ICAL_FOYER={r.ical_foyer}",
    ]
    lignes += [f"{variable_ical(a)}={a.ical}" for a in r.adultes]
    return "\n".join(lignes) + "\n"


def identite_md(r: Reponses) -> str:
    def adulte(a: Adulte) -> str:
        return f"{a.prenom} ({'propriétaire' if a.role == 'proprietaire' else 'membre'} du système)"

    enfants = ", ".join(f"{e['prenom']}" + (f" ({e['age']})" if e["age"] else "") for e in r.enfants) or "aucun déclaré"
    animaux = ", ".join(f"{a['nom']}" + (f" ({a['espece']})" if a["espece"] else "") for a in r.animaux) or "aucun déclaré"
    return (
        "# Identité du foyer\n"
        f"_Pré-remplie à l'installation le {date.today().isoformat()} ; à compléter et faire valider lors de la séance 1._\n\n"
        "## Qui vit ici\n"
        f"- Adultes : {', '.join(adulte(a) for a in r.adultes)}\n"
        f"- Enfants : {enfants}\n"
        f"- Animaux : {animaux}\n"
        f"- Commune : {r.commune or 'non renseignée'}\n\n"
        "## Rythme de la semaine\n- À demander lors de la séance 1.\n\n"
        "## Courses\n- Magasins habituels : à demander lors de la séance 1.\n"
    )


def choix_env(r: Reponses) -> str:
    def b(v: bool) -> str:
        return "oui" if v else "non"

    personnes = " ".join(f"{a.linux_user}:{a.auth}" for a in r.adultes)
    return "\n".join(
        [
            f"NOM_ASSISTANT={json.dumps(r.assistant, ensure_ascii=False)}",
            f"HOTE_TAILSCALE={slug(r.assistant) or 'firmin'}",
            f"PERSONNES='{personnes}'",
            f"KITCHEN_DAILY={b(r.kitchen_daily)}",
            f"TRANSPORTS={b(r.transports)}",
            f"RESEAUX='{r.reseaux}'",
            f"TAILSCALE={b(r.tailscale)}",
            f"TAILSCALE_CLE='{r.tailscale_cle}'",
            f"VAULTWARDEN={b(r.vaultwarden)}",
            f"HORS_SITE={b(r.hors_site)}",
            f"TELEGRAM={b(bool(r.telegram_token))}",
            f"CLE_API={b(bool(r.cle_api))}",
            f"GROUPE={b(r.groupe_telegram_id is not None)}",
        ]
    ) + "\n"


def ecrire(r: Reponses, sortie: Path) -> None:
    sortie.mkdir(parents=True, exist_ok=True)
    os.chmod(sortie, 0o700)
    for nom, contenu, mode in (
        ("config.yaml", config_yaml(r), 0o600),
        ("secrets.env", secrets_env(r), 0o600),
        ("identite.md", identite_md(r), 0o600),
        ("choix.env", choix_env(r), 0o600),
    ):
        chemin = sortie / nom
        fd = os.open(chemin, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contenu)
        os.chmod(chemin, mode)


# --- questionnaire interactif -----------------------------------------------------------------------------------


def titre(texte: str) -> None:
    print(f"\n\033[1;34m── {texte} ──\033[0m")


def dire(texte: str) -> None:
    print("   " + texte.replace("\n", "\n   "))


def demander(question: str, valider=None, defaut: str | None = None, secret: bool = False, facultatif: bool = False):
    """Pose une question jusqu'à obtenir une réponse valide. Entrée vide : défaut, ou "" si facultatif."""
    suffixe = f" [{defaut}]" if defaut else (" (Entrée pour passer)" if facultatif else "")
    while True:
        try:
            if secret:
                import getpass

                brut = getpass.getpass(f" ? {question}{suffixe} : ")
            else:
                brut = input(f" ? {question}{suffixe} : ")
        except (EOFError, KeyboardInterrupt):
            raise Abandon() from None
        brut = brut.strip()
        if not brut:
            if defaut is not None:
                brut = defaut
            elif facultatif:
                return ""
            else:
                print("   (réponse attendue)")
                continue
        try:
            return valider(brut) if valider else brut
        except ValueError as exc:
            print(f"   ✗ {exc}")


def interactif() -> Reponses:
    r = Reponses()
    titre("Bienvenue")
    dire(
        "Je vais vous poser une vingtaine de questions pour installer votre assistant de famille.\n"
        "Gardez à portée de main : l'application Telegram, le compte Claude de chaque adulte (ou une clé API),\n"
        "et une demi-heure. Rien de ce que vous tapez ne quitte ce serveur. Ctrl+C pour arrêter à tout moment."
    )

    titre("1. L'assistant")
    r.assistant = demander("Quel prénom voulez-vous donner à l'assistant ?", prenom_assistant, defaut=NOM_ASSISTANT_DEFAUT)
    r.tutoiement = demander("Il vous tutoie ? (oui/non)", oui_non, defaut="oui")

    titre("2. Le foyer")
    dire("La commune sert à la météo du jour. Jamais d'adresse : la commune suffit.")
    r.commune = demander("Dans quelle commune habitez-vous ?", facultatif=True)
    nb = demander("Combien d'adultes utiliseront l'assistant ? (1 à 6)", lambda t: _borne(t, 1, 6), defaut="2")
    dire(
        "Pour chaque adulte : son prénom, son rôle et son identifiant Telegram.\n"
        "Identifiant Telegram : dans Telegram, écrire à @userinfobot, il répond avec un nombre (Id)."
    )
    vus: set[str] = set()
    for i in range(int(nb)):
        print(f"\n   Adulte {i + 1}")
        prenom = demander("Prénom")
        propose = slug(prenom)
        while propose in vus or identifiant_valide(propose):
            propose = f"{propose}-{i + 1}"[:32]
        ident = demander(
            "Identifiant technique (compte sur le serveur)", lambda t: _ident(t, vus), defaut=propose
        )
        vus.add(ident)
        dire("Rôle : propriétaire = peut régler le système en parlant à l'assistant ; membre = l'utilise seulement.")
        role = demander(
            "Rôle (proprietaire/membre)", lambda t: _choix(t, ROLES), defaut="proprietaire"
        )
        tid = demander(f"Identifiant Telegram de {prenom}", telegram_id)
        dire(
            "Compte Claude : « abonnement » = son propre abonnement Claude (Pro ou Max), connecté après\n"
            "l'installation ; « cle_api » = les conversations passent par la clé API du foyer (payée à l'usage)."
        )
        auth = demander("Compte Claude (abonnement/cle_api)", lambda t: _choix(t, ("abonnement", "cle_api")), defaut="abonnement")
        r.adultes.append(Adulte(prenom, ident, role, tid, auth))
    if not any(a.role == "proprietaire" for a in r.adultes):
        dire(f"Il faut au moins un propriétaire : {r.adultes[0].prenom} le devient.")
        r.adultes[0].role = "proprietaire"

    dire(
        "\nLes enfants : seulement le prénom et un âge approximatif ou la classe (« 8 ans », « CE2 »).\n"
        "Pas de date de naissance, pas d'école, rien de médical."
    )
    while demander("Ajouter un enfant ? (oui/non)", oui_non, defaut="non"):
        r.enfants.append({"prenom": demander("Prénom de l'enfant"), "age": demander("Âge approximatif ou classe", facultatif=True)})
    while demander("Ajouter un animal ? (oui/non)", oui_non, defaut="non"):
        r.animaux.append({"nom": demander("Nom de l'animal"), "espece": demander("Espèce (chien, chat…)", facultatif=True)})

    titre("3. Telegram")
    dire(
        "Créez le bot : dans Telegram, écrire à @BotFather → /newbot → choisir un nom → copier le « token ».\n"
        "Puis créez un groupe avec les adultes et le bot, ajoutez @userinfobot au groupe pour lire son\n"
        "identifiant (négatif, commence par -100), et retirez-le. Dans @BotFather : /setprivacy → Disable."
    )
    r.telegram_token = demander("Token du bot", token_telegram, secret=True, facultatif=True)
    g = demander("Identifiant du groupe du foyer", lambda t: telegram_id(t, groupe=True), facultatif=True)
    r.groupe_telegram_id = g if g != "" else None

    titre("4. Claude pour les messages programmés")
    dire(
        "Les briefs du matin et de la semaine partent tout seuls : ils utilisent une clé API Anthropic\n"
        "(console.anthropic.com → API keys, quelques euros par mois). Sans clé, pas de brief automatique."
    )
    r.cle_api = demander("Clé API Anthropic", cle_api, secret=True, facultatif=True)
    if any(a.auth == "cle_api" for a in r.adultes) and not r.cle_api:
        dire("Attention : un adulte utilise la clé API ; sans clé, il ne pourra pas parler à l'assistant.")
    r.brief_jour = demander("Heure du brief du matin", heure, defaut="7h15")
    r.brief_hebdo = demander("Jour et heure du brief de la semaine", jour_heure, defaut="dim 19h")

    titre("5. Agendas (facultatif)")
    dire("Google Agenda → Paramètres de l'agenda → « Adresse secrète au format iCal ». Entrée pour passer.")
    r.ical_foyer = demander("Agenda commun du foyer", url_ical, secret=True, facultatif=True)
    for a in r.adultes:
        a.ical = demander(f"Agenda de {a.prenom}", url_ical, secret=True, facultatif=True)

    titre("6. Capacités")
    r.kitchen_daily = demander("Êtes-vous abonnés aux plats livrés Kitchen Daily ? (oui/non)", oui_non, defaut="non")
    dire("Transports : horaires et retards sur vos trajets habituels, rappel des trains et vols notés à l'agenda.\n"
         "Le train SNCF est toujours disponible ; un réseau local (bus, car, métro, tram) s'ajoute s'il est connu,\n"
         "ou plus tard en conversation (« ajoute le métro de Lyon »).")
    r.transports = demander("Activer les transports ? (oui/non)", oui_non, defaut="oui")
    if r.transports:
        dire(f"Réseaux locaux déjà connus : {', '.join(f'{n} ({t})' for n, t in RESEAUX_LOCAUX.items())}.")
        r.reseaux = demander("Réseau local utilisé (nom, ou Entrée si aucun de ceux-là)", reseaux, facultatif=True)

    titre("7. Appli web, coffre et sauvegarde")
    dire(
        "Tailscale relie vos téléphones au serveur sans rien ouvrir sur Internet ; il faut un compte gratuit\n"
        "(tailscale.com) et l'app Tailscale sur chaque téléphone. Il permet l'appli web et le coffre à mots de passe."
    )
    r.tailscale = demander("Installer Tailscale et l'appli web ? (oui/non)", oui_non, defaut="oui")
    if r.tailscale:
        dire("Facultatif : une clé d'authentification (console Tailscale → Settings → Keys) évite d'ouvrir un lien.")
        r.tailscale_cle = demander("Clé Tailscale (tskey-…)", cle_tailscale, secret=True, facultatif=True)
        r.vaultwarden = demander("Installer aussi le coffre à mots de passe Vaultwarden ? (oui/non)", oui_non, defaut="non")
    dire("Une sauvegarde chiffrée est faite chaque nuit sur le serveur. Une copie ailleurs (Google Drive…) protège\n"
         "d'une panne du serveur ; elle se configure à la fin de l'installation (5 minutes).")
    r.hors_site = demander("Configurer la copie hors du serveur à la fin ? (oui/non)", oui_non, defaut="oui")
    return r


def _borne(t: str, bas: int, haut: int) -> str:
    if not t.isdigit() or not bas <= int(t) <= haut:
        raise ValueError(f"nombre entre {bas} et {haut}")
    return t


def _choix(t: str, choix) -> str:
    t = t.strip().lower().replace("é", "e").replace("è", "e").replace(" ", "_")
    if t not in choix:
        raise ValueError("réponses possibles : " + ", ".join(choix))
    return t


def _ident(t: str, vus: set[str]) -> str:
    if err := identifiant_valide(t):
        raise ValueError(err)
    if t in vus:
        raise ValueError("déjà utilisé pour un autre adulte")
    return t


def recapitulatif(r: Reponses) -> str:
    def b(v: bool) -> str:
        return "oui" if v else "non"

    lignes = [
        f"Assistant      : {r.assistant} ({'tutoiement' if r.tutoiement else 'vouvoiement'})",
        f"Commune        : {r.commune or '(non renseignée)'}",
    ]
    for a in r.adultes:
        lignes.append(f"Adulte         : {a.prenom} — compte « {a.linux_user} », {a.role}, Telegram {a.telegram_id}, Claude {a.auth}")
    lignes += [
        f"Enfants        : {', '.join(e['prenom'] for e in r.enfants) or 'aucun'}",
        f"Animaux        : {', '.join(a['nom'] for a in r.animaux) or 'aucun'}",
        f"Telegram       : token {'saisi' if r.telegram_token else 'à faire'}, groupe {r.groupe_telegram_id or 'à faire'}",
        f"Clé API        : {'saisie' if r.cle_api else 'à faire (pas de brief automatique sans elle)'}",
        f"Briefs         : matin {r.brief_jour}, semaine {r.brief_hebdo}",
        f"Capacités      : Kitchen Daily {b(r.kitchen_daily)}, transports {b(r.transports)}"
        + (f" (réseau local : {r.reseaux})" if r.transports and r.reseaux else ""),
        f"Tailscale      : {b(r.tailscale)} ; Vaultwarden {b(r.vaultwarden)} ; copie hors site {b(r.hors_site)}",
    ]
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Questionnaire d'installation d'un foyer")
    p.add_argument("--sortie", required=True, type=Path)
    p.add_argument("--reponses", type=Path, help="réponses JSON (sans question)")
    args = p.parse_args(argv)
    try:
        if args.reponses:
            r = depuis_dict(json.loads(args.reponses.read_text(encoding="utf-8")))
        else:
            r = interactif()
            titre("Récapitulatif")
            dire(recapitulatif(r))
            if not demander("C'est bon, on installe ? (oui/non)", oui_non, defaut="oui"):
                print("Rien n'a été écrit. Relancez l'installation quand vous voulez.")
                return 1
    except Abandon:
        print("\nInstallation interrompue : rien n'a été écrit.")
        return 1
    except (ValueError, KeyError) as exc:
        print(f"Réponses invalides : {exc}", file=sys.stderr)
        return 2
    ecrire(r, args.sortie)
    if args.reponses:
        print(recapitulatif(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Écriture contrôlée de `data/config.yaml` (lot 7a, administration par la conversation).

Toute modification passe par `modifier(cle, valeur, auteur)` :

1. la clé doit être dans la liste blanche (`CLES_AUTORISEES`) : jamais d'identité, de secret ni d'agenda ;
2. le fichier est relu sous verrou, la clé modifiée (commentaires conservés avec ruamel.yaml s'il est installé) ;
3. le résultat est **validé** : `bot.config.parse_config` doit réussir sans écarter de personne, horaires et
   modes de validation doivent être lisibles (`valider`) ; sinon rien n'est écrit ;
4. sauvegarde `config.yaml.bak-<horodatage>` (les 10 dernières sont gardées), écriture atomique ;
5. une ligne dans le journal d'administration `data/administration/journal.jsonl` (auteur, clé, avant, après),
   qui permet le retour arrière (`tools.administration.annuler`).

Le journal reçoit aussi les autres réglages de l'administration (activation d'une capacité, paramètre, pause) :
`journaliser(...)`. CLI (pour `scripts/appliquer-config.sh`) :

    python -m tools.config_edition timers-foyer     # brief_hebdo<TAB>OnCalendar, brief_jour<TAB>OnCalendar
    python -m tools.config_edition valider          # code 1 et message si config.yaml est invalide
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import io
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from tools.common import data_dir, get_logger, iso_now, now

log = get_logger("homeos.config_edition")

NB_SAUVEGARDES = 10
CRONS_FOYER = {"brief_hebdo": "Sun 19:00", "brief_jour": "07:15"}
MODES_VALIDATION = ("humaine", "automatique")
_SEG = r"[a-z0-9_]+"
# Liste blanche (spec lot 7a §1). `capacites.*.parametres.*` est accepté pour une surcharge dans config.yaml,
# mais les réglages déclarés des capacités vivent dans data/capacites/<nom>/parametres.yaml (admin_parametre).
CLES_AUTORISEES = (
    re.compile(rf"^capacites\.{_SEG}\.validation$"),
    re.compile(rf"^capacites\.{_SEG}\.crons\.{_SEG}$"),
    re.compile(rf"^crons\.{_SEG}$"),
    re.compile(rf"^capacites\.{_SEG}\.parametres\.{_SEG}$"),
)
# Jamais modifiables, même si un motif ci-dessus les couvrait (crons.porteur_linux_user est informatif).
CLES_INTERDITES = frozenset({"crons.porteur_linux_user"})
_MOTIF_SECRET = re.compile(r"(secret|token|mot_de_passe|password|cle_api|api_key|ical|url)", re.IGNORECASE)


class ConfigInvalide(ValueError):
    """La configuration obtenue serait invalide : rien n'a été écrit (message en français)."""


class CleRefusee(ValueError):
    """Clé hors liste blanche."""


# --- chemins -------------------------------------------------------------------------


def _donnees(donnees: Path | None) -> Path:
    return Path(donnees) if donnees is not None else data_dir()


def chemin_config(donnees: Path | None = None) -> Path:
    return _donnees(donnees) / "config.yaml"


def dossier_administration(donnees: Path | None = None) -> Path:
    dossier = _donnees(donnees) / "administration"
    if not dossier.exists():
        dossier.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(dossier, 0o2770)
    return dossier


def chemin_journal(donnees: Path | None = None) -> Path:
    return dossier_administration(donnees) / "journal.jsonl"


@contextlib.contextmanager
def verrou(donnees: Path | None = None) -> Iterator[None]:
    """Verrou exclusif des écritures de l'administration (config.yaml et journal)."""
    with open(dossier_administration(donnees) / ".verrou", "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- liste blanche -------------------------------------------------------------------


def verifier_cle(cle: str) -> list[str]:
    """Découpe une clé pointée autorisée (`capacites.kitchen_daily.crons.rappel`) ; lève CleRefusee sinon."""
    cle = str(cle or "").strip()
    if cle in CLES_INTERDITES or not any(m.fullmatch(cle) for m in CLES_AUTORISEES):
        raise CleRefusee(f"Réglage « {cle} » non modifiable par la conversation.")
    if _MOTIF_SECRET.search(cle):
        raise CleRefusee(f"Réglage « {cle} » refusé : jamais de secret ni d'adresse dans la configuration.")
    return cle.split(".")


def _valeur_sans_secret(valeur: Any) -> None:
    if isinstance(valeur, (dict, list)):
        raise ConfigInvalide("Valeur refusée : un réglage simple (texte, nombre, oui/non) est attendu.")
    if isinstance(valeur, str) and (len(valeur) > 200 or re.search(r"https?://|\n", valeur)):
        raise ConfigInvalide("Valeur refusée : texte court sans adresse web attendu.")


# --- lecture / écriture YAML (commentaires conservés si possible) ---------------------------


def _ruamel() -> Any | None:
    try:
        from ruamel.yaml import YAML  # type: ignore[import-not-found]
    except ImportError:
        return None
    rt = YAML(typ="rt")
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=4, offset=2)
    return rt


def lire_brut(donnees: Path | None = None) -> dict[str, Any]:
    """config.yaml en dictionnaire simple (lève ConfigInvalide si illisible)."""
    chemin = chemin_config(donnees)
    try:
        brut = yaml.safe_load(chemin.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigInvalide(f"Configuration introuvable : {chemin}") from None
    except yaml.YAMLError as exc:
        raise ConfigInvalide(f"config.yaml illisible ({exc.__class__.__name__}).") from None
    if not isinstance(brut, dict):
        raise ConfigInvalide("config.yaml invalide : un dictionnaire YAML est attendu.")
    return brut


def valeur(brut: dict[str, Any], cle: str) -> Any:
    """Valeur d'une clé pointée (None si absente)."""
    courant: Any = brut
    for morceau in cle.split("."):
        if not isinstance(courant, dict) or morceau not in courant:
            return None
        courant = courant[morceau]
    return courant


def _affecter(doc: Any, morceaux: list[str], nouvelle: Any) -> None:
    """Affecte (ou supprime si `nouvelle` est None) dans un mapping imbriqué ; nettoie les blocs vidés."""
    pile = [doc]
    courant = doc
    for morceau in morceaux[:-1]:
        suivant = courant.get(morceau) if hasattr(courant, "get") else None
        if suivant is None:
            if nouvelle is None:
                return
            suivant = {}
            courant[morceau] = suivant
        elif not hasattr(suivant, "get"):
            raise ConfigInvalide(f"« {morceau} » n'est pas un bloc de réglages dans config.yaml.")
        courant = suivant
        pile.append(courant)
    if nouvelle is None:
        courant.pop(morceaux[-1], None)
        for parent, morceau in zip(reversed(pile[:-1]), reversed(morceaux[:-1]), strict=True):
            enfant = parent.get(morceau)
            if hasattr(enfant, "__len__") and len(enfant) == 0:
                parent.pop(morceau, None)
            else:
                break
    else:
        courant[morceaux[-1]] = nouvelle


def _rendre(texte_actuel: str, morceaux: list[str], nouvelle: Any) -> str:
    """Nouveau texte de config.yaml : ruamel (commentaires conservés), sinon PyYAML avec un en-tête."""
    rt = _ruamel()
    if rt is not None:
        doc = rt.load(texte_actuel) if texte_actuel.strip() else None
        if doc is None:
            from ruamel.yaml.comments import CommentedMap  # type: ignore[import-not-found]

            doc = CommentedMap()
        if isinstance(nouvelle, str):
            # Toujours entre guillemets : « 18:00 » non cité serait relu comme un nombre par PyYAML (YAML 1.1).
            from ruamel.yaml.scalarstring import DoubleQuotedScalarString  # type: ignore[import-not-found]

            nouvelle = DoubleQuotedScalarString(nouvelle)
        _affecter(doc, morceaux, nouvelle)
        sortie = io.StringIO()
        rt.dump(doc, sortie)
        return sortie.getvalue()
    brut = yaml.safe_load(texte_actuel) or {}
    _affecter(brut, morceaux, nouvelle)
    entete = (
        "# Firmin Assistant — configuration réécrite par l'administration par la conversation\n"
        "# (commentaires d'origine dans la sauvegarde config.yaml.bak-* la plus ancienne)\n"
    )
    return entete + yaml.safe_dump(brut, allow_unicode=True, sort_keys=False, default_flow_style=False)


# --- validation -------------------------------------------------------------------------


def valider(brut: dict[str, Any], donnees: Path | None = None) -> None:
    """Lève ConfigInvalide si `brut` ne ferait pas une configuration correcte pour le bot et les crons."""
    from bot.config import ROLES, parse_config
    from tools.capacites import oncalendar

    if not isinstance(brut, dict):
        raise ConfigInvalide("La configuration doit être un dictionnaire.")
    try:
        cfg = parse_config(copy.deepcopy(brut), _donnees(donnees), Path(__file__).resolve().parent.parent)
    except Exception as exc:  # noqa: BLE001 - toute erreur de lecture rend la configuration invalide
        raise ConfigInvalide(f"Configuration refusée par le bot ({exc.__class__.__name__} : {exc}).") from None
    personnes = brut.get("personnes") or []
    if not isinstance(personnes, list) or len(cfg.personnes) != len(personnes):
        raise ConfigInvalide("Configuration refusée : une personne du foyer deviendrait illisible.")
    for p in personnes:
        role = (p or {}).get("role") if isinstance(p, dict) else None
        if role is not None and str(role) not in ROLES:
            raise ConfigInvalide(f"Rôle inconnu « {role} » (attendu : {', '.join(ROLES)}).")
    crons = brut.get("crons") or {}
    if not isinstance(crons, dict):
        raise ConfigInvalide("`crons` doit être un bloc de réglages.")
    for nom, quand in crons.items():
        if nom == "porteur_linux_user":
            continue
        try:
            oncalendar(str(quand))
        except ValueError as exc:
            raise ConfigInvalide(f"Horaire « {nom} » invalide : {exc}") from None
    capacites = brut.get("capacites") or {}
    if not isinstance(capacites, dict):
        raise ConfigInvalide("`capacites` doit être un bloc de réglages.")
    for nom, reglages in capacites.items():
        if reglages is None:
            continue
        if not isinstance(reglages, dict):
            raise ConfigInvalide(f"`capacites.{nom}` doit être un bloc de réglages.")
        mode = reglages.get("validation")
        if mode is not None and str(mode).lower() not in (*MODES_VALIDATION, "auto"):
            raise ConfigInvalide(f"Validation « {mode} » inconnue pour {nom} (humaine ou automatique).")
        crons_cap = reglages.get("crons") or {}
        if not isinstance(crons_cap, dict):
            raise ConfigInvalide(f"`capacites.{nom}.crons` doit être un bloc de réglages.")
        for cron, quand in crons_cap.items():
            try:
                oncalendar(str(quand))
            except ValueError as exc:
                raise ConfigInvalide(f"Horaire « {nom}/{cron} » invalide : {exc}") from None


# --- journal ------------------------------------------------------------------------------


def lire_journal(donnees: Path | None = None) -> list[dict[str, Any]]:
    chemin = chemin_journal(donnees)
    entrees: list[dict[str, Any]] = []
    try:
        lignes = chemin.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for ligne in lignes:
        try:
            entree = json.loads(ligne)
        except json.JSONDecodeError:
            continue
        if isinstance(entree, dict) and isinstance(entree.get("id"), int):
            entrees.append(entree)
    return entrees


def journaliser(entree: dict[str, Any], donnees: Path | None = None) -> dict[str, Any]:
    """Ajoute une entrée (id croissant, horodatage) au journal ; à appeler sous `verrou()`."""
    entrees = lire_journal(donnees)
    complete = {"id": (max((e["id"] for e in entrees), default=0) + 1), "quand": iso_now(), **entree}
    chemin = chemin_journal(donnees)
    with open(chemin, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(complete, ensure_ascii=False, default=str) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(chemin, 0o664)
    return complete


# --- sauvegardes -----------------------------------------------------------------------------


def sauvegardes(donnees: Path | None = None) -> list[Path]:
    """Sauvegardes de config.yaml, de la plus ancienne à la plus récente (`bak-<horodatage>[-<n>]`)."""
    return sorted(_donnees(donnees).glob("config.yaml.bak-*"), key=_ordre_sauvegarde)


def _ordre_sauvegarde(chemin: Path) -> tuple[str, int]:
    m = re.fullmatch(r"config\.yaml\.bak-(\d{8}-\d{6})(?:-(\d+))?", chemin.name)
    return (m.group(1), int(m.group(2) or 1)) if m else (chemin.name, 0)


def _sauvegarder(chemin: Path) -> Path:
    horodatage = now().strftime("%Y%m%d-%H%M%S")
    # Même seconde : compteur après le plus grand existant (jamais un nom libéré par le ménage, qui serait mal trié).
    meme = [_ordre_sauvegarde(s)[1] for s in sauvegardes(chemin.parent) if _ordre_sauvegarde(s)[0] == horodatage]
    n = max(meme, default=0) + 1
    cible = chemin.with_name(f"config.yaml.bak-{horodatage}" + (f"-{n}" if n > 1 else ""))
    cible.write_bytes(chemin.read_bytes())
    with contextlib.suppress(OSError):
        os.chmod(cible, 0o664)
    for ancienne in sauvegardes(chemin.parent)[:-NB_SAUVEGARDES]:
        with contextlib.suppress(OSError):
            ancienne.unlink()
    return cible


def _ecrire_atomique(chemin: Path, texte: str) -> None:
    tmp = chemin.with_name(f".{chemin.name}.{os.getpid()}.tmp")
    tmp.write_text(texte, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o664)  # groupe foyer : bot (homeos) et outils (personnes) relisent la configuration
    os.replace(tmp, chemin)


# --- modification ----------------------------------------------------------------------------


def modifier(
    cle: str,
    nouvelle: Any,
    auteur: str,
    resume: str = "",
    donnees: Path | None = None,
    annule: int | None = None,
) -> dict[str, Any]:
    """Modifie une clé autorisée de config.yaml (None = supprimer) ; renvoie l'entrée du journal.

    Lève CleRefusee (clé hors liste blanche) ou ConfigInvalide (rien n'est écrit)."""
    morceaux = verifier_cle(cle)
    _valeur_sans_secret(nouvelle)
    chemin = chemin_config(donnees)
    with verrou(donnees):
        try:
            texte = chemin.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ConfigInvalide(f"Configuration introuvable : {chemin}") from None
        avant_brut = lire_brut(donnees)
        avant = valeur(avant_brut, cle)
        nouveau_texte = _rendre(texte, morceaux, nouvelle)
        try:
            apres_brut = yaml.safe_load(nouveau_texte) or {}
        except yaml.YAMLError as exc:
            raise ConfigInvalide(f"Écriture refusée : YAML produit illisible ({exc.__class__.__name__}).") from None
        valider(apres_brut, donnees)
        if valeur(apres_brut, cle) != nouvelle:
            raise ConfigInvalide("Écriture refusée : la valeur relue ne correspond pas à la valeur demandée.")
        sauvegarde = _sauvegarder(chemin)
        _ecrire_atomique(chemin, nouveau_texte)
        entree: dict[str, Any] = {
            "type": "config",
            "auteur": auteur,
            "cle": cle,
            "avant": avant,
            "apres": nouvelle,
            "resume": resume or f"{cle} : {avant!r} → {nouvelle!r}",
            "sauvegarde": sauvegarde.name,
        }
        if annule is not None:
            entree["annule"] = annule
        entree = journaliser(entree, donnees)
    log.info("config.yaml : %s modifié par %s (journal #%s)", cle, auteur, entree["id"])
    return entree


# --- CLI ----------------------------------------------------------------------------------------


def timers_foyer(donnees: Path | None = None) -> list[tuple[str, str]]:
    """(cron, OnCalendar) des briefs, depuis config.yaml (défauts du contrat si absent ou invalide)."""
    from tools.capacites import oncalendar

    try:
        crons = lire_brut(donnees).get("crons") or {}
    except ConfigInvalide as exc:
        log.warning("%s : horaires par défaut", exc)
        crons = {}
    lignes: list[tuple[str, str]] = []
    for nom, defaut in CRONS_FOYER.items():
        quand = crons.get(nom, defaut) if isinstance(crons, dict) else defaut
        try:
            lignes.append((nom, oncalendar(str(quand))))
        except ValueError:
            log.warning("crons.%s=%r invalide, défaut %s utilisé", nom, quand, defaut)
            lignes.append((nom, oncalendar(defaut)))
    return lignes


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["timers-foyer"]:
        for nom, cal in timers_foyer():
            sys.stdout.write(f"{nom}\t{cal}\n")
        return 0
    if args == ["valider"]:
        try:
            valider(lire_brut())
        except ConfigInvalide as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        sys.stdout.write("config.yaml valide\n")
        return 0
    sys.stderr.write("usage : python -m tools.config_edition timers-foyer|valider\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())

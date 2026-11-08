"""Validations, indépendamment du canal : motif ``CONFIRMATION_REQUISE:<id>:<résumé>`` et décision Valider / Annuler.

- :func:`extraire_confirmations` détecte le motif dans le texte final, le retire et renvoie les actions à confirmer ;
- :func:`decider` applique « valider » (``python -m tools.runner execute <id>``) ou « annuler » (``cancelled`` dans
  ``actions_log``) avec les mêmes règles pour tous les canaux : pause du foyer, action déjà traitée, actions
  d'administration réservées aux propriétaires (lot 7a) ;
- :func:`executer_automatiquement` : capacités en ``validation: automatique`` (sauf ``confirmation_toujours``) ;
- les lignes ``CAPTURE:<chemin>`` du résultat (Kitchen Daily) sont retirées et confiées au canal
  (``envoyer_captures``) : photo dans le groupe Telegram, carte ``capture`` dans l'appli web. Seuls les fichiers sous
  le dossier des captures Kitchen Daily sont acceptés (:func:`capture_autorisee`).

Le bot Telegram (``bot/confirmations.py``) ajoute les boutons inline et l'envoi des photos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot import db as dbmod
from bot.config import Config, Personne
from coeur.verrous import VerrouFichier

log = logging.getLogger(__name__)

# `CONFIRMATION_REQUISE:12:Ajouter « Dentiste » le 12/03 à 10h` — le résumé va jusqu'à la fin de ligne.
_MOTIF = re.compile(r"^[ \t]*`?CONFIRMATION_REQUISE:(\d+):([^\n`]*)`?[ \t]*$", re.MULTILINE)
_MOTIF_INLINE = re.compile(r"`?CONFIRMATION_REQUISE:(\d+):([^\n`]*)`?")

_MOTIF_CAPTURE = re.compile(r"^[ \t]*CAPTURE:(\S[^\n]*?)[ \t]*$", re.MULTILINE)

# L'application d'une sélection Kitchen Daily (navigateur headless, ~20 clics, relecture) peut durer.
EXECUTE_TIMEOUT_S = 300

# Variables transmises au runner en plus de HOMEOS_DATA / PATH (Playwright a besoin de HOME et des navigateurs).
_ENV_TRANSMIS = ("HOME", "LANG", "PLAYWRIGHT_BROWSERS_PATH", "HOMEOS_ROOT")

# Lot 7a : actions d'administration (outils admin_*) — validées par un propriétaire seulement ;
# « reprendre » reste validable pendant la pause.
PREFIXE_ADMIN = "admin_"
# Lot 5c : actions sur le coffre (renommer, attribuer, supprimer, ranger une quarantaine) — jamais automatiques,
# step-up dans l'appli, validées par le demandeur ou un propriétaire du foyer (la quarantaine : le demandeur seul).
PREFIXE_IDENTIFIANTS = "identifiants_"
MESSAGE_IDENTIFIANTS = "Seule la personne qui l'a demandé (ou un propriétaire du foyer) peut valider ce changement du coffre."
ACTIONS_HORS_PAUSE = frozenset({"admin_reprendre"})
MESSAGE_RESERVE = "Ce réglage est réservé aux propriétaires du foyer."
# Lot 7b : première action avec effet d'une capacité fabriquée par l'atelier (en essai) — un propriétaire seulement,
# jamais automatique, step-up dans l'appli ; sa réussite active la capacité (tools.capacites).
MESSAGE_PREMIERE = (
    "Cette capacité vient de l'atelier et n'a encore jamais servi : sa première action est validée par un "
    "propriétaire du foyer."
)
MESSAGE_PAUSE = "Le foyer est en pause (/reprendre pour réactiver) : aucune action n'est exécutée."

# Envoi des captures d'une action validée : (chemins autorisés, légende) -> nombre envoyé.
EnvoyerCaptures = Callable[[list[Path], str], Awaitable[int]]
Executer = Callable[[Config, int], Awaitable[tuple[bool, str]]]
Traiter = Callable[[str, str], Awaitable[str]]


@dataclass(frozen=True)
class Confirmation:
    action_id: int
    resume: str


def extraire_confirmations(texte: str) -> tuple[str, list[Confirmation]]:
    """Retire les motifs du texte et renvoie ``(texte_nettoye, confirmations)``.

    Le motif est d'abord cherché sur une ligne entière (cas normal), puis en ligne (si le
    modèle l'a collé dans une phrase). Les doublons d'``id`` sont fusionnés.
    """
    if "CONFIRMATION_REQUISE:" not in texte:
        return texte, []
    trouvees: dict[int, Confirmation] = {}

    def _collect(m: re.Match[str]) -> str:
        aid = int(m.group(1))
        resume = m.group(2).strip().rstrip("`").strip()
        if aid not in trouvees:
            trouvees[aid] = Confirmation(aid, resume)
        return ""

    nettoye = _MOTIF.sub(_collect, texte)
    nettoye = _MOTIF_INLINE.sub(_collect, nettoye)
    # Compacte les lignes vides laissées par le retrait.
    nettoye = re.sub(r"\n{3,}", "\n\n", nettoye).strip()
    return nettoye, list(trouvees.values())


def texte_confirmation(confs: list[Confirmation]) -> str:
    if len(confs) == 1:
        return f"À valider : {confs[0].resume or 'action #' + str(confs[0].action_id)}"
    lignes = ["À valider :"]
    for c in confs:
        lignes.append(f"• #{c.action_id} — {c.resume or 'action'}")
    return "\n".join(lignes)


def parser_callback(data: str) -> tuple[str, int] | None:
    """``confirm:12`` → ``("confirm", 12)`` ; ``None`` si la donnée n'est pas reconnue."""
    m = re.fullmatch(r"(confirm|cancel):(\d+)", data or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


async def executer_action(cfg: Config, action_id: int) -> tuple[bool, str]:
    """Lance ``python -m tools.runner execute <id>`` (sous l'utilisateur du service, cwd=racine)."""
    cmd = [cfg.python_bin, "-m", "tools.runner", "execute", str(action_id)]
    env = {"HOMEOS_DATA": str(cfg.data_dir), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    env.update({k: os.environ[k] for k in _ENV_TRANSMIS if os.environ.get(k)})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cfg.root_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=EXECUTE_TIMEOUT_S)
    except FileNotFoundError:
        return False, "Le runner des outils est introuvable (tools/runner.py)."
    except TimeoutError:
        return False, "L'exécution de l'action a dépassé le délai, elle est peut-être incomplète."
    sortie = out.decode("utf-8", "replace").strip()
    erreur = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        log.warning("runner execute %s : code %s, stderr=%s", action_id, proc.returncode, erreur[-500:])
        derniere = erreur.splitlines()[-1][:300] if erreur else ""
        return False, sortie or derniere or "échec sans message"
    return True, sortie or "Action exécutée."


def extraire_captures(texte: str) -> tuple[str, list[Path]]:
    """Retire les lignes ``CAPTURE:<chemin>`` du texte et renvoie ``(texte_nettoye, chemins)``."""
    if "CAPTURE:" not in texte:
        return texte, []
    chemins = [Path(m.group(1).strip()) for m in _MOTIF_CAPTURE.finditer(texte)]
    nettoye = _MOTIF_CAPTURE.sub("", texte)
    nettoye = re.sub(r"\n{3,}", "\n\n", nettoye).strip()
    return nettoye, chemins


def capture_autorisee(cfg: Config, chemin: Path) -> bool:
    """Fichier existant situé sous les captures Kitchen Daily (aucun autre fichier ne peut sortir par ce biais)."""
    try:
        racine = cfg.kd_captures_dir.resolve()
        cible = chemin.resolve()
    except OSError:
        return False
    return racine in cible.parents and cible.is_file()


def captures_autorisees(cfg: Config, chemins: list[Path]) -> list[Path]:
    valides = [c for c in chemins if capture_autorisee(cfg, c)]
    for c in chemins:
        if c not in valides:
            log.warning("capture ignorée (absente ou hors du dossier des captures) : %s", c.name)
    return valides


def premiere_validation(cfg: Config, kind: str) -> bool:
    """Lot 7b : le kind est une action avec effet d'une capacité de l'assistant encore en essai (jamais validée)."""
    if not kind or kind.startswith((PREFIXE_ADMIN, PREFIXE_IDENTIFIANTS)):
        return False
    try:
        from tools import capacites

        return capacites.premiere_validation_pour(kind, donnees=cfg.data_dir) is not None
    except Exception:
        log.debug("première validation : capacité du kind %s illisible", kind, exc_info=True)
        return False


def kind_sensible(cfg: Config, kind: str) -> bool:
    """Action qui demande une preuve d'identité récente dans l'appli web (step-up, CONTRACT §12) :
    administration, actions jamais automatiques (``confirmation_toujours`` : extras payants…) et première action
    d'une capacité fabriquée par l'atelier."""
    return (kind.startswith((PREFIXE_ADMIN, PREFIXE_IDENTIFIANTS))
            or kind in cfg.actions_confirmation_toujours()
            or premiere_validation(cfg, kind))


def peut_valider_identifiants(action: Any, personne: Personne | None) -> bool:
    """Action ``identifiants_*`` : le demandeur, ou un propriétaire du foyer (sauf pour ranger une quarantaine,
    réservée à la personne qui a envoyé la donnée). Sans personne connue (exécution interne) : accepté."""
    if personne is None:
        return True
    try:
        payload = json.loads(action["payload"] or "{}") if isinstance(action["payload"], str) else dict(action["payload"])
    except (ValueError, TypeError, KeyError, IndexError):
        return False
    demandeur = str(payload.get("demandeur") or "")
    if personne.linux_user == demandeur:
        return True
    return personne.proprietaire and str(action["kind"]) != "identifiants_ranger_quarantaine"


async def decider(
    cfg: Config,
    db: dbmod.Db,
    verbe: str,
    action_id: int,
    who: str,
    *,
    personne: Personne | None = None,
    executer: Executer | None = None,
    envoyer_captures: EnvoyerCaptures | None = None,
) -> str:
    """Valide (``verbe="confirm"``) ou annule (``"cancel"``) une action en attente ; renvoie le texte à afficher.

    ``personne`` : qui décide (une action d'administration n'est validée que par un propriétaire). ``executer`` :
    exécution d'une action validée (défaut :func:`executer_action`). ``envoyer_captures`` : le canal affiche les
    captures signalées par ``CAPTURE:<chemin>`` (déjà filtrées par :func:`capture_autorisee`)."""
    if verbe not in ("confirm", "cancel"):
        return "Bouton inconnu."
    action = await dbmod.run(db.action, action_id)
    kind = str(action["kind"]) if action is not None else ""
    if cfg.pause_path.exists() and not (verbe == "confirm" and kind in ACTIONS_HORS_PAUSE):
        return MESSAGE_PAUSE
    if action is None:
        return f"Action #{action_id} introuvable."
    statut = action["status"]
    if statut != dbmod.STATUS_PENDING:
        return f"Action #{action_id} déjà traitée (état : {statut})."
    if verbe == "confirm" and kind.startswith(PREFIXE_ADMIN) and personne is not None and not personne.proprietaire:
        await dbmod.run(db.log, who, "confirmation", f"validation #{action_id} refusée (rôle)", None, "ignored")
        return MESSAGE_RESERVE
    if verbe == "confirm" and kind.startswith(PREFIXE_IDENTIFIANTS) and not peut_valider_identifiants(action, personne):
        await dbmod.run(db.log, who, "confirmation", f"validation #{action_id} refusée (coffre)", None, "ignored")
        return MESSAGE_IDENTIFIANTS
    premiere = verbe == "confirm" and premiere_validation(cfg, kind)
    if premiere and (personne is None or not personne.proprietaire):
        await dbmod.run(db.log, who, "confirmation", f"validation #{action_id} refusée (première action)", None, "ignored")
        return MESSAGE_PREMIERE

    if verbe == "cancel":
        ok = await dbmod.run(db.annuler_action, action_id, who)
        await dbmod.run(db.log, who, "confirmation", f"annulation #{action_id}", None, "ok" if ok else "error")
        return f"❌ Action #{action_id} annulée." if ok else f"Action #{action_id} n'était plus en attente."

    # Un seul exécuteur à la fois par action, entre processus (bouton Telegram et appli web) : le statut est relu
    # sous le verrou, la seconde validation trouve l'action déjà traitée au lieu de l'exécuter une deuxième fois.
    async with VerrouFichier(cfg.etat_dir / "verrous" / f"action-{action_id}.lock"):
        action = await dbmod.run(db.action, action_id)
        if action is None or action["status"] != dbmod.STATUS_PENDING:
            statut = action["status"] if action is not None else "introuvable"
            return f"Action #{action_id} déjà traitée (état : {statut})."
        if premiere and personne is not None:
            from tools import capacites

            # Autorise CETTE action seulement ; l'exécuteur de la capacité active la capacité si elle réussit.
            await asyncio.to_thread(
                capacites.autoriser_premiere_validation, kind, action_id, personne.linux_user, None, cfg.data_dir
            )
        ok, message = await (executer or executer_action)(cfg, action_id)
        message, captures = extraire_captures(message)
        if captures and envoyer_captures is not None:
            envoyees = await envoyer_captures(captures_autorisees(cfg, captures), f"Capture — action #{action_id}")
            if envoyees:
                message += "\n📸 Capture d'écran envoyée."
        await dbmod.run(
            db.log,
            who,
            "confirmation",
            f"validation #{action_id}",
            None,
            dbmod.STATUS_OK if ok else dbmod.STATUS_ERROR,
            message[:500],
        )
        if ok:
            # Le runner met normalement le statut à jour ; on sécurise si ce n'est pas le cas.
            apres = await dbmod.run(db.action, action_id)
            if apres is not None and apres["status"] == dbmod.STATUS_PENDING:
                await dbmod.run(db.maj_statut, action_id, dbmod.STATUS_DONE, message[:500])
            return f"✅ Validé par {who}.\n{message}"
        await dbmod.run(db.maj_statut, action_id, dbmod.STATUS_ERROR, message[:500])
        return f"⚠️ L'action #{action_id} a échoué : {message}"


async def executer_automatiquement(
    cfg: Config, db: dbmod.Db, confs: list[Confirmation], who: str, traiter: Traiter
) -> tuple[list[Confirmation], list[str]]:
    """Exécute sans bouton les actions dont la capacité est en ``validation: automatique``.

    Renvoie ``(confirmations restant à valider, textes de résultat des actions automatiques)``.
    ``traiter(data, who)`` est le chemin du bouton Valider du canal (``data`` = ``confirm:<id>``) : seul le clic
    humain est remplacé par la décision de configuration. Les kinds listés dans ``confirmation_toujours`` d'un
    manifeste (ex. extras payants, semaine sautée) gardent toujours le bouton.
    """
    restantes: list[Confirmation] = []
    resultats: list[str] = []
    toujours = cfg.actions_confirmation_toujours() if confs else set()
    for conf in confs:
        action = await dbmod.run(db.action, conf.action_id)
        kind = str(action["kind"]) if action else ""
        if (not action or kind in toujours or kind.startswith(PREFIXE_IDENTIFIANTS)
                or not cfg.action_automatique(kind) or premiere_validation(cfg, kind)):
            restantes.append(conf)
            continue
        texte = await traiter(f"confirm:{conf.action_id}", f"{who} (automatique)")
        resultats.append(texte.replace(f"✅ Validé par {who} (automatique).", "✅ Fait automatiquement."))
    return restantes, resultats


def payload_action(action: Any) -> dict[str, Any]:
    """Ligne ``actions_log`` → dictionnaire simple (id, kind, résumé, statut), sans le payload."""
    if action is None:
        return {}
    return {"id": action["id"], "kind": action["kind"], "resume": action["summary"], "statut": action["status"]}

"""Routeur de modèle (lot 4a) : quel modèle Claude pour ce message ?

1. Règles, sans IA (instantané, gratuit) :
   - commande (``/bus``, ``/kd …``) → ``simple`` ;
   - mots de décision (choisis, compare, décide, sélection…) → ``decision`` ;
   - confirmation (« oui », « ok », « vas-y », « c'est bon »…) → ``simple`` ;
   - message court qui commence par un verbe d'action connu (« ajoute du lait », « coche les œufs ») → ``simple`` ;
   - fichier reçu (lecture d'un document) → ``standard``.
2. Sinon, classification par un appel Haiku très court (``claude --bare -p`` avec la clé API, réponse
   ``simple|standard|decision``, délai 3 s) ; repli ``standard`` sur délai dépassé, erreur ou réponse illisible.

Niveau → modèle : ``ia.modele_simple`` (haiku), ``ia.modele`` (sonnet), ``ia.modele_decisions`` (opus) ;
``ia.routeur: false`` donne toujours ``ia.modele``. Le texte du message n'est jamais journalisé ici (seulement
le niveau et la raison) ; il n'est transmis qu'à Claude, comme le message lui-même.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from tools.modeles import NIVEAUX, ReglagesIA

log = logging.getLogger(__name__)

DELAI_CLASSIFICATION_S = 3.0
MOTS_MAX_COURT = 12
CARACTERES_MAX_COURT = 100
LONGUEUR_MAX_CLASSIFIEE = 600

PREFIXES_CANAL = ("[vocal transcrit]",)

# Verbes d'action des outils du foyer (forme normalisée : minuscules, sans accents), à l'impératif ou à l'infinitif.
VERBES_ACTION = frozenset(
    re.split(
        r"\s+",
        """
    ajoute ajouter rajoute note noter notes coche cocher decoche decocher retire retirer enleve enlever supprime
    supprimer efface effacer mets mettre rappelle rappelle-moi rappeler montre montre-moi montrer affiche afficher
    liste lister lis lire donne donne-moi envoie envoyer ouvre ouvrir range ranger vide vider annule annuler
    valide valider programme programmer planifie planifier reserve reserver inscris inscrire marque marquer
    garde garder enregistre enregistrer active activer desactive desactiver arrete arreter lance lancer
    quand quel quelle quels quelles combien
    """.strip(),
    )
)
# Le message entier (normalisé, ponctuation retirée) est une confirmation.
_CONFIRMATIONS_PHRASES = frozenset(
    {
        "oui", "non", "ok", "okay", "d'accord", "dac", "daccord", "vas-y", "vas y", "go", "valide", "je valide",
        "confirme", "c'est bon", "cest bon", "parfait", "merci", "merci beaucoup", "super", "top", "nickel",
        "ca marche", "exactement", "tout a fait", "carrement", "bien sur", "oui merci", "non merci", "oui vas-y",
        "oui stp", "oui s'il te plait", "annule", "laisse tomber", "stop", "ok merci", "ok parfait", "oui parfait",
        "c'est parfait", "ca me va", "ok pour moi", "on y va", "fais-le", "fais le", "valide-le",
    }
)
_MOTS_DECISION = re.compile(
    r"\b(choisi[rst]?|choisissez|choix|compare[rsz]?|comparai(son|sons)|comparatif|decide[rsz]?|decision|"
    r"selection(ne|nes|ner|nez)?|arbitre[rz]?|recommande[rsz]?|lequel|laquelle|lesquels|lesquelles|"
    r"vaut[- ]il mieux|plutot)\b"
)
_INVITES_POLIES = re.compile(r"^(peux[- ]tu|tu peux|pourrais[- ]tu|merci de|stp|s'il te plait)\s+")

CONSIGNE = (
    "Classe la demande ci-dessous, adressée à l'assistant d'un foyer, en un seul mot :\n"
    "- simple : action ou question courte et directe (ajouter à une liste, lire l'agenda, confirmer) ;\n"
    "- standard : conversation, explication, rédaction, plusieurs étapes ;\n"
    "- decision : choisir, comparer, arbitrer entre des options, planifier en tenant compte de préférences.\n"
    "Réponds uniquement par simple, standard ou decision.\n\n<demande>\n{texte}\n</demande>"
)

Classifieur = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class Route:
    niveau: str  # simple | standard | decision
    modele: str
    raison: str  # commande | decision | confirmation | action_courte | fichier | classification | repli | inactif


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, espaces simplifiés, préfixes de canal retirés."""
    t = (texte or "").strip()
    for prefixe in PREFIXES_CANAL:
        if t.lower().startswith(prefixe):
            t = t[len(prefixe) :].strip()
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii").lower()
    t = t.replace("’", "'")
    return " ".join(t.split())


def niveau_par_regles(texte: str) -> tuple[str, str] | None:
    """``(niveau, raison)`` si une règle s'applique, sinon ``None`` (classification nécessaire)."""
    brut = (texte or "").strip()
    if "[fichier reçu:" in brut:
        return "standard", "fichier"
    t = normaliser(brut)
    if not t:
        return "simple", "confirmation"
    if t.startswith("/"):
        return "simple", "commande"
    if _MOTS_DECISION.search(t):
        return "decision", "decision"
    sans_ponctuation = re.sub(r"[!?.…,;:]+", " ", t)
    sans_ponctuation = " ".join(sans_ponctuation.split())
    if sans_ponctuation in _CONFIRMATIONS_PHRASES:
        return "simple", "confirmation"
    mots = sans_ponctuation.split()
    if len(mots) <= MOTS_MAX_COURT and len(t) <= CARACTERES_MAX_COURT:
        reste = _INVITES_POLIES.sub("", sans_ponctuation)
        premier = (reste.split() or [""])[0]
        if premier in VERBES_ACTION:
            return "simple", "action_courte"
    return None


def lire_niveau(reponse: str | None) -> str | None:
    """Premier mot reconnu (simple, standard, decision) d'une réponse du classifieur."""
    t = normaliser(reponse or "")
    for mot in re.findall(r"[a-z]+", t):
        if mot in NIVEAUX:
            return mot
    return None


def commande_classification(exe: str, texte: str, modele: str, reglages: str) -> list[str]:
    """``claude --bare -p`` : un tour, modèle simple, sortie JSON, lecture bloquée hors du dossier vide."""
    prompt = CONSIGNE.format(texte=texte[:LONGUEUR_MAX_CLASSIFIEE])
    return [exe, "--bare", "-p", prompt, "--output-format", "json", "--max-turns", "1", "--model", modele,
            "--settings", reglages]


def classifieur_claude(
    modele: str, reglages: str, cwd: Path, delai_s: float = DELAI_CLASSIFICATION_S
) -> Classifieur:
    """Classifieur par défaut : ``claude --bare -p`` avec ``ANTHROPIC_API_KEY`` (sinon ``None`` sans appel)."""

    async def classer(texte: str) -> str | None:
        from coeur.claude import environnement_claude

        cle = os.environ.get("ANTHROPIC_API_KEY")
        exe = shutil.which("claude")
        if not cle or not exe:
            return None
        env = environnement_claude(cle_api=True)
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            proc = await asyncio.create_subprocess_exec(
                *commande_classification(exe, texte, modele, reglages),
                cwd=str(cwd) if cwd.is_dir() else "/",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except OSError:
            return None
        try:
            sortie, _ = await asyncio.wait_for(proc.communicate(), timeout=delai_s)
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            log.info("classification du message : délai de %.0f s dépassé, repli standard", delai_s)
            return None
        if proc.returncode != 0:
            return None
        try:
            obj = json.loads(sortie.decode("utf-8", "replace"))
        except ValueError:
            return sortie.decode("utf-8", "replace")
        if isinstance(obj, dict):
            return str(obj.get("result") or "")
        return None

    return classer


async def choisir(texte: str, ia: ReglagesIA, classifieur: Classifieur | None = None) -> Route:
    """Route d'un message : règles, puis classification (repli ``standard``)."""
    if not ia.routeur:
        return Route("standard", ia.modele, "inactif")
    regle = niveau_par_regles(texte)
    if regle is not None:
        niveau, raison = regle
        return Route(niveau, ia.pour_niveau(niveau), raison)
    niveau = None
    if classifieur is not None:
        try:
            niveau = lire_niveau(await asyncio.wait_for(classifieur(texte), timeout=DELAI_CLASSIFICATION_S + 0.5))
        except TimeoutError:
            niveau = None
        except Exception:
            log.warning("classification du message impossible, repli standard", exc_info=True)
            niveau = None
    if niveau is None:
        return Route("standard", ia.modele, "repli")
    return Route(niveau, ia.pour_niveau(niveau), "classification")

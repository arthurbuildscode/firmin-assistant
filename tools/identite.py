"""Identité de l'appelant des outils (lot 7) : qui parle, quel que soit le transport MCP.

- Serveur stdio (développement, repli) : les outils tournent sous l'utilisateur Linux de la personne ; l'identité
  est `HOMEOS_PERSONNE` (tests, bot en clé API) ou l'uid du processus, comme avant le lot 7.
- Serveur HTTP persistant (`homeos-outils.service`, sous homeos) : l'identité vient du jeton présenté par la
  session Claude (`tools.serveur_http`), placée pour la durée de la requête dans une `ContextVar`. Elle prime sur
  tout le reste : l'environnement du service et l'uid (homeos) ne disent rien de la personne qui parle.

Lue par `tools.capacites.personne_courante()` (et donc `ctx.personne`, `ctx.secret()`), `tools.agenda.
utilisateur_courant()` et `tools.common.who()` (colonne `who` d'`actions_log`). Les outils synchrones sont exécutés
dans un fil séparé (`en_thread`) avec une copie du contexte : la `ContextVar` les suit.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
import pwd
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

# Utilisateur Linux de l'appelant d'une requête HTTP authentifiée (None hors requête).
_UTILISATEUR: contextvars.ContextVar[str | None] = contextvars.ContextVar("homeos_utilisateur", default=None)

RE_UTILISATEUR = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def utilisateur_requete() -> str | None:
    """Utilisateur Linux résolu depuis le jeton de la requête en cours, None hors requête HTTP."""
    return _UTILISATEUR.get()


def en_requete() -> bool:
    """Vrai pendant une requête HTTP authentifiée : l'identité de la requête fait foi."""
    return _UTILISATEUR.get() is not None


@contextmanager
def requete(utilisateur: str) -> Iterator[None]:
    """Place `utilisateur` (linux_user, ou « homeos » pour le bot et les crons) comme appelant le temps du bloc."""
    if not RE_UTILISATEUR.match(utilisateur or ""):
        raise ValueError("utilisateur invalide")
    jeton = _UTILISATEUR.set(utilisateur)
    try:
        yield
    finally:
        _UTILISATEUR.reset(jeton)


def utilisateur_courant() -> str | None:
    """Utilisateur Linux qui parle : requête HTTP, sinon `HOMEOS_PERSONNE`, sinon l'uid du processus.

    L'uid, et non `$USER`, fait foi en stdio : le modèle ne choisit ni l'un ni l'autre, mais l'uid ne dépend pas de
    l'environnement hérité."""
    requete_en_cours = _UTILISATEUR.get()
    if requete_en_cours is not None:
        return requete_en_cours
    env = os.environ.get("HOMEOS_PERSONNE", "").strip()
    if env:
        return env
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return None


def personne_de(utilisateur: str | None, config: dict[str, Any]) -> dict[str, Any] | None:
    """Entrée `personnes[]` de config.yaml dont le `linux_user` est `utilisateur` (None : homeos, inconnu)."""
    if not utilisateur:
        return None
    for p in config.get("personnes") or []:
        if isinstance(p, dict) and str(p.get("linux_user") or "") == utilisateur:
            return p
    return None


def auteur_requete() -> str | None:
    """Auteur à inscrire dans `actions_log.who` pour la requête en cours : prénom de la personne (sinon son
    linux_user) ; « assistant » pour le jeton homeos ; None hors requête HTTP."""
    utilisateur = _UTILISATEUR.get()
    if utilisateur is None:
        return None
    from tools.common import load_config  # import tardif : tools.common importe ce module

    p = personne_de(utilisateur, load_config())
    if p is None:
        return "assistant"
    return str(p.get("prenom") or utilisateur)


# --- exécution des outils synchrones hors de la boucle asyncio ------------------------------------------------


def signature_evaluee(fn: Callable[..., Any]) -> inspect.Signature | None:
    """Signature de `fn` (enveloppes suivies) avec les annotations évaluées dans le module d'origine.

    Les modules utilisent `from __future__ import annotations` : sans cette évaluation, le SDK mcp résoudrait les
    annotations textuelles dans les variables globales de l'enveloppe, et non dans celles de l'outil."""
    try:
        return inspect.signature(fn, eval_str=True)
    except Exception:  # noqa: BLE001 - annotation non résoluble : le SDK fera comme avant
        return None


def en_thread(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Version asynchrone d'un outil synchrone, exécutée dans un fil de travail avec une copie du contexte.

    - le serveur HTTP reste disponible pendant un outil lent (Kitchen Daily, 20 s) ;
    - l'identité de la requête (`ContextVar`) est copiée dans le fil ;
    - nom, description et schéma d'entrée sont ceux de l'outil d'origine.
    Un outil déjà asynchrone est rendu tel quel."""
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def asynchrone(*args: Any, **kwargs: Any) -> Any:
        import anyio.to_thread

        contexte = contextvars.copy_context()
        return await anyio.to_thread.run_sync(functools.partial(contexte.run, fn, *args, **kwargs))

    signature = signature_evaluee(fn)
    if signature is not None:
        asynchrone.__signature__ = signature  # type: ignore[attr-defined]
        annotations = {
            nom: p.annotation for nom, p in signature.parameters.items() if p.annotation is not inspect.Parameter.empty
        }
        if signature.return_annotation is not inspect.Signature.empty:
            annotations["return"] = signature.return_annotation
        asynchrone.__annotations__ = annotations
    return asynchrone

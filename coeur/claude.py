"""Cœur de la conversation : lancement de ``claude -p`` (Claude Code) sous l'utilisateur Linux de la personne.

Lot 4a : extrait de ``bot/claude_runner.py`` (qui réexporte ces noms) pour être partagé par les canaux Telegram
(``bot/``) et appli web (``web/``). Aucun import de Telegram ici.

Commande construite comme au §5 du CONTRACT : ``sudo -n -H -u <personne> /usr/local/sbin/homeos-claude``
(wrapper root qui impose les options de sécurité et refuse le reste, lot 5d), ``cwd`` vide
(``/var/lib/homeos-cwd/<personne>``), ``--add-dir`` limité aux sous-dossiers de données non sensibles +
``content/`` + ``capabilities/``, ``--settings`` (lecture bloquée hors de ces dossiers, refus explicites des
chemins sensibles), ``--include-partial-messages`` (indispensable pour recevoir les ``text_delta``).
La sortie ``stream-json`` est lue ligne à ligne en asyncio ; coût, jetons et durée de l'événement ``result``
sont remontés dans :class:`ClaudeResult` (journalisés par le bot, revue P2).

Usage :

    runner = ClaudeRunner(cfg)
    run = runner.run(linux_user="<personne>", prenom="…", prompt="...", session_id=None, canal="web")
    async for ev in run.evenements():   # texte (deltas), outil (tool_use), resultat_outil (tool_result)
        ...
    resultat = run.result  # ClaudeResult(text, session_id, ...)

``run.deltas()`` (canal Telegram) ne rend que les morceaux de texte, comme avant le lot 4a.

Erreurs : :class:`ClaudeError` (message utilisateur en français dans ``.message``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.config import Config
from tools.common import proxy_sortant
from tools.modeles import modele_valide
from tools.securite import OUTILS_AUTORISES, dossiers_lisibles, reglages_json

LIMITE_LIGNE = 32 * 1024 * 1024  # taille max d'une ligne stream-json (images en base64)

log = logging.getLogger(__name__)

TIMEOUT_S = 180
PLACEHOLDER_NOM = "{{assistant.nom}}"

_AUTH_PATTERNS = re.compile(
    r"(not logged in|please run /login|claude auth login|invalid api key|authentication[_ ]?(error|failed)|"
    r"oauth.*(expir|invalid|revoked)|token.*(expir|invalid|revoked)|401|unauthori[sz]ed)",
    re.IGNORECASE,
)
_MISSING_PATTERNS = re.compile(r"(command not found|no such file or directory|not found)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Erreurs
# ---------------------------------------------------------------------------
class ClaudeError(Exception):
    """Erreur avec un message destiné à l'utilisateur (français, sans secret)."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ClaudeMissing(ClaudeError):
    pass


class ClaudeNotConnected(ClaudeError):
    pass


class ClaudeTimeout(ClaudeError):
    pass


# ---------------------------------------------------------------------------
# Parsing stream-json (fonctions pures, testables)
# ---------------------------------------------------------------------------
@dataclass
class StreamEvent:
    kind: str  # "init" | "text_delta" | "assistant_text" | "tool_use" | "result" | "other"
    text: str = ""
    session_id: str | None = None
    is_error: bool = False
    subtype: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_line(line: str) -> StreamEvent | None:
    """Transforme une ligne ``stream-json`` en :class:`StreamEvent` (``None`` si vide/illisible)."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        log.debug("Ligne non JSON ignorée : %.200s", line)
        return None
    if not isinstance(obj, dict):
        return None
    typ = obj.get("type")
    sid = obj.get("session_id")

    if typ == "system":
        return StreamEvent("init" if obj.get("subtype") == "init" else "other", session_id=sid, raw=obj)

    if typ == "stream_event":
        ev = obj.get("event") or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return StreamEvent("text_delta", text=str(delta["text"]), session_id=sid, raw=obj)
        return StreamEvent("other", session_id=sid, raw=obj)

    if typ == "assistant":
        content = (obj.get("message") or {}).get("content") or []
        texts: list[str] = []
        tool = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                texts.append(str(block["text"]))
            elif block.get("type") == "tool_use":
                tool = True
        if texts:
            return StreamEvent("assistant_text", text="".join(texts), session_id=sid, raw=obj)
        if tool:
            return StreamEvent("tool_use", session_id=sid, raw=obj)
        return StreamEvent("other", session_id=sid, raw=obj)

    if typ == "result":
        text = obj.get("result")
        if not isinstance(text, str):
            text = ""
        return StreamEvent(
            "result",
            text=text,
            session_id=sid,
            is_error=bool(obj.get("is_error")),
            subtype=obj.get("subtype"),
            raw=obj,
        )

    return StreamEvent("other", session_id=sid, raw=obj)


CANAUX = ("telegram", "web")


@dataclass
class EvenementClaude:
    """Événement utile aux canaux, tiré du flux ``stream-json`` :

    - ``texte`` : morceau de réponse (``texte``) ;
    - ``outil`` : appel d'outil (``outil`` = nom complet ``mcp__homeos__…``, ``outil_id``, ``entree``) ;
    - ``resultat_outil`` : résultat d'un appel (``outil_id``, ``texte`` concaténé, ``erreur``).
    """

    kind: str
    texte: str = ""
    outil: str | None = None
    outil_id: str | None = None
    entree: dict[str, Any] = field(default_factory=dict)
    erreur: bool = False


def blocs_outils(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Blocs ``tool_use`` d'un événement ``assistant`` (``[]`` sinon)."""
    if raw.get("type") != "assistant":
        return []
    contenu = (raw.get("message") or {}).get("content") or []
    return [b for b in contenu if isinstance(b, dict) and b.get("type") == "tool_use"]


def _texte_resultat(contenu: Any) -> str:
    if isinstance(contenu, str):
        return contenu
    morceaux: list[str] = []
    if isinstance(contenu, list):
        for bloc in contenu:
            if isinstance(bloc, dict) and bloc.get("type") == "text" and isinstance(bloc.get("text"), str):
                morceaux.append(bloc["text"])
            elif isinstance(bloc, str):
                morceaux.append(bloc)
    return "\n".join(morceaux)


def blocs_resultats(raw: dict[str, Any]) -> list[EvenementClaude]:
    """Résultats d'outils (``tool_result``) d'un événement ``user`` du flux (``[]`` sinon)."""
    if raw.get("type") != "user":
        return []
    contenu = (raw.get("message") or {}).get("content") or []
    if not isinstance(contenu, list):
        return []
    sortie: list[EvenementClaude] = []
    for bloc in contenu:
        if isinstance(bloc, dict) and bloc.get("type") == "tool_result":
            sortie.append(
                EvenementClaude(
                    "resultat_outil",
                    texte=_texte_resultat(bloc.get("content")),
                    outil_id=str(bloc.get("tool_use_id") or "") or None,
                    erreur=bool(bloc.get("is_error")),
                )
            )
    return sortie


@dataclass
class ClaudeResult:
    text: str
    session_id: str | None
    is_error: bool = False
    subtype: str | None = None
    tool_calls: int = 0
    cout_usd: float | None = None
    tokens_entree: int | None = None
    tokens_sortie: int | None = None
    duree_ms: int | None = None

    def mesures(self) -> dict[str, Any]:
        """Coût, jetons et durée pour ``actions_log`` (valeurs absentes omises)."""
        brut = {
            "cout_usd": self.cout_usd,
            "tokens_entree": self.tokens_entree,
            "tokens_sortie": self.tokens_sortie,
            "duree_ms": self.duree_ms,
        }
        return {k: v for k, v in brut.items() if v is not None}


def mesures_resultat(raw: dict[str, Any]) -> dict[str, Any]:
    """Extrait ``total_cost_usd``, ``usage`` et ``duration_ms`` d'un événement ``result`` (tolérant)."""

    def nombre(v: Any, conv: Any) -> Any:
        try:
            return conv(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    entree = nombre(usage.get("input_tokens"), int)
    for cle in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        extra = nombre(usage.get(cle), int)
        if extra is not None:
            entree = (entree or 0) + extra
    return {
        "cout_usd": nombre(raw.get("total_cost_usd"), float),
        "tokens_entree": entree,
        "tokens_sortie": nombre(usage.get("output_tokens"), int),
        "duree_ms": nombre(raw.get("duration_ms"), int),
    }


def est_erreur_auth(texte: str) -> bool:
    return bool(_AUTH_PATTERNS.search(texte or ""))


# ---------------------------------------------------------------------------
# Prompt système : substitution {{assistant.nom}} et {{foyer.adultes}}
# ---------------------------------------------------------------------------
def substituer_nom(texte: str, nom: str, config: dict | None = None) -> str:
    """Variables des prompts : ``{{assistant.nom}}`` et, si la configuration est fournie, ``{{foyer.adultes}}``
    (``tools.common.rendre_gabarit``)."""
    if config is None:
        return texte.replace(PLACEHOLDER_NOM, nom)
    from tools.common import rendre_gabarit

    return rendre_gabarit(texte, config, nom=nom)


def fragments_capacites(cfg: Config) -> str:
    """Fragments ``CLAUDE.md`` des capacités chargées (lot 5a) ; ``""`` si aucune ou en cas d'erreur."""
    try:
        from tools import capacites

        return capacites.fragments_prompt(racine=cfg.capabilities_dir, donnees=cfg.data_dir)
    except Exception:
        log.warning("Fragments des capacités indisponibles", exc_info=True)
        return ""


TAILLE_MAX_MEMOIRE = 6000  # caractères par fichier de mémoire injecté dans le prompt


def _lire_tronque(chemin: Path) -> str:
    try:
        texte = chemin.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(texte) > TAILLE_MAX_MEMOIRE:
        texte = texte[:TAILLE_MAX_MEMOIRE].rstrip() + "\n[… suite tronquée : lis le fichier avec memoire_lire]"
    return texte


def contexte_memoire(cfg: Config, linux_user: str | None) -> str:
    """Ce que l'assistant doit savoir AVANT de répondre : identité et règles du foyer, fiche de la personne,
    modules d'onboarding déjà faits. Relu à chaque message, donc toujours à jour, et jamais « oublié »
    quand la session Claude repart de zéro. Vide si rien n'existe encore."""
    data = cfg.data_dir
    blocs: list[str] = []
    for rel, titre in (
        ("foyer/identite.md", "Identité du foyer (foyer/identite.md)"),
        ("foyer/regles.md", "Règles du foyer (foyer/regles.md)"),
    ):
        texte = _lire_tronque(data / rel)
        if texte:
            blocs.append(f"### {titre}\n{texte}")
    if linux_user:
        texte = _lire_tronque(data / "personnes" / f"{linux_user}.md")
        if texte:
            blocs.append(f"### Fiche de la personne qui te parle (personnes/{linux_user}.md)\n{texte}")
    faits: list[str] = []
    try:
        import json

        for f in sorted((data / "onboarding").glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if obj.get("done"):
                reponses = obj.get("answers") or obj.get("reponses") or {}
                resume = (
                    " ; ".join(f"{k} : {str(v)[:300]}" for k, v in reponses.items())
                    if isinstance(reponses, dict)
                    else str(reponses)[:1200]
                )
                faits.append(f"- {f.stem} : fait" + (f" — {resume}" if resume else ""))
    except OSError:
        pass
    if faits:
        blocs.append("### Modules de découverte déjà faits (ne repose pas ces questions)\n" + "\n".join(faits))
    if not blocs:
        return ""
    return (
        "## Ce que tu sais déjà (mémoire du foyer, relue à chaque message)\n\n"
        "Ces informations ont été données par le foyer : utilise-les sans les redemander. Avant toute question "
        "(onboarding compris), vérifie qu'elle n'y est pas déjà répondue ; si une réponse partielle s'y trouve, "
        "propose-la et demande seulement confirmation.\n\n" + "\n\n".join(blocs)
    )


def chemin_prompt_systeme(cfg: Config, linux_user: str | None) -> Path:
    base = cfg.system_prompt_path
    if not linux_user:
        return base
    return base.with_name(f"{base.stem}-{linux_user}{base.suffix}")


def preparer_prompt_systeme(cfg: Config, linux_user: str | None = None) -> Path:
    """``content/CLAUDE.md`` + fragments des capacités actives + mémoire du foyer et de la personne,
    ``{{assistant.nom}}`` remplacé, écrit (atomiquement) dans ``data/.claude-system-prompt[-<personne>].md``."""
    source = cfg.system_prompt_source
    if source.exists():
        texte = source.read_text(encoding="utf-8")
    else:
        log.warning("content/CLAUDE.md introuvable (%s) : prompt système minimal", source)
        texte = f"Tu es {PLACEHOLDER_NOM}, l'assistant du foyer. Réponds en français, brièvement."
    fragments = fragments_capacites(cfg)
    if fragments:
        texte = texte.rstrip() + "\n\n" + fragments
    autos = sorted(cfg.capacites_automatiques())
    if autos:
        texte = texte.rstrip() + (
            "\n\n## Validation automatique\n\n"
            f"Pour les capacités suivantes, le foyer a choisi de te laisser la main : {', '.join(autos)}. "
            "Leurs actions avec effet sont exécutées immédiatement par le bot dès que tu appelles l'outil, sans bouton "
            "Valider : tu laisses la mention CONFIRMATION_REQUISE telle quelle, mais tu annonces « je l'enregistre » "
            "(pas « à valider ci-dessous »), le résultat réel suit dans le même message. Tu restes responsable de la "
            "qualité du choix : préférences, variété, historique. Un changement demandé ensuite se traite de la même façon."
        )
    memoire = contexte_memoire(cfg, linux_user)
    if memoire:
        texte = texte.rstrip() + "\n\n" + memoire
    cible = chemin_prompt_systeme(cfg, linux_user)
    cible.parent.mkdir(parents=True, exist_ok=True)
    tmp = cible.with_name(f"{cible.name}.{os.getpid()}.tmp")
    tmp.write_text(substituer_nom(texte, cfg.assistant_nom, cfg.raw), encoding="utf-8")
    try:
        os.chmod(tmp, 0o664)
    except OSError:
        pass
    os.replace(tmp, cible)
    return cible


# ---------------------------------------------------------------------------
# Construction de la commande (§5)
# ---------------------------------------------------------------------------
def construire_commande(
    cfg: Config,
    linux_user: str,
    prompt: str,
    session_id: str | None,
    *,
    claude_bin: str = "claude",
    cle_api: bool = False,
    mcp_config: Path | None = None,
    canal: str = "telegram",
    modele: str | None = None,
) -> list[str]:
    """Commande ``claude -p``.

    ``modele`` (lot 4a, routeur de modèle) : ``--model <alias|identifiant>`` si le nom passe la liste blanche
    (``tools.modeles.MOTIF_MODELE``, la même que le wrapper) ; sinon, ou ``None`` : modèle par défaut de Claude Code.

    ``canal`` (lot 4a) : ``web`` ajoute ``--homeos-canal web`` pour le wrapper (en-tête ``X-Homeos-Canal`` vers le
    serveur d'outils, qui ajoute alors les lignes ``CARTE:`` des cartes génératives) ; ``telegram`` (défaut) ne
    change rien à la commande des lots précédents.

    - ``cle_api=False`` : session Claude Code de la personne → ``sudo -n -H -u <linux_user> <wrapper>`` ; le
      wrapper root (``/usr/local/sbin/homeos-claude``) revérifie chaque option et refuse tout le reste.
    - ``cle_api=True`` : clé API (``ANTHROPIC_API_KEY`` dans l'environnement du bot), ``--bare``,
      exécutée directement sous l'utilisateur du bot : rien ne touche au compte Claude de la personne.
    - ``HOMEOS_NO_SUDO=1`` (développement) : ``claude`` lancé directement, mêmes options.
    - ``mcp_config`` (lot 7, clé API avec le serveur d'outils HTTP) : configuration MCP générée pour la personne
      (son jeton), passée avec ``--strict-mcp-config`` à la place de ``tools/mcp.json``. En mode abonnement, le
      wrapper fait la même substitution sous la personne.
    """
    cmd: list[str] = []
    if cle_api:
        cmd += [claude_bin if cfg.no_sudo else (shutil.which("claude") or "claude"), "--bare"]
    elif cfg.no_sudo:
        cmd += [claude_bin]
    else:
        cmd += ["sudo", "-n", "-H", "-u", linux_user, str(cfg.wrapper_path)]
        if canal == "web":
            cmd += ["--homeos-canal", "web"]
    if prompt.startswith("-"):
        # `claude -p <message>` : un message qui commence par un tiret serait lu comme une option. Les canaux
        # préfixent toujours une ligne de contexte ; ceinture et bretelles (le wrapper refuse aussi).
        prompt = " " + prompt
    cmd += ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    if modele and modele_valide(modele):
        cmd += ["--model", modele]
    if session_id:
        cmd += ["--resume", session_id]
    reglages = reglages_json(
        cfg.data_dir,
        cfg.etat_dir,
        cfg.secrets_dir,
        personne=linux_user,
        personnes=[p.linux_user for p in cfg.personnes],
    )
    cmd += [
        "--append-system-prompt-file",
        str(chemin_prompt_systeme(cfg, linux_user)),
        "--mcp-config",
        str(mcp_config or cfg.mcp_config_path),
        *(["--strict-mcp-config"] if mcp_config else []),
        "--allowedTools",
        OUTILS_AUTORISES,
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--settings",
        reglages,
    ]
    # Lecture limitée aux dossiers non sensibles (fichiers reçus, photos, mémoire, contenus, onboarding des capacités).
    for dossier in dossiers_lisibles(cfg.data_dir, cfg.root_dir):
        cmd += ["--add-dir", str(dossier)]
    cmd += ["--include-partial-messages"]
    return cmd


def config_mcp_http(cfg: Config, linux_user: str, canal: str = "telegram") -> Path:
    """Lot 7, personne en ``auth: cle_api`` (claude lancé sous homeos) : configuration MCP HTTP avec le jeton de la
    personne (lu dans ``/etc/homeos/outils-jetons.env``), écrite en 600 dans ``<etat>/outils/mcp-<personne>.json``
    (``/var/lib/homeos`` : 700 homeos). Le jeton n'apparaît jamais dans les arguments.

    Canal web (lot 4a) : fichier distinct ``mcp-<personne>-web.json`` avec l'en-tête ``X-Homeos-Canal: web``."""
    from tools.common import FICHIER_JETONS, parse_env, reglages_outils

    try:
        jetons = parse_env((cfg.secrets_dir / FICHIER_JETONS).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ClaudeError(
            "Le serveur d'outils n'a pas de jeton lisible pour le bot (relancer install.sh).", f"{FICHIER_JETONS} : {exc}"
        ) from exc
    jeton = jetons.get(f"JETON_{linux_user}", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", jeton):
        raise ClaudeError(
            f"Aucun jeton du serveur d'outils pour {linux_user} (relancer install.sh).", f"JETON_{linux_user} absent"
        )
    entetes = {"Authorization": f"Bearer {jeton}"}
    suffixe = ""
    if canal == "web":
        entetes["X-Homeos-Canal"] = "web"
        suffixe = "-web"
    contenu = {"mcpServers": {"homeos": {"type": "http", "url": reglages_outils()["url"], "headers": entetes}}}
    dossier = cfg.etat_dir / "outils"
    dossier.mkdir(mode=0o700, parents=True, exist_ok=True)
    cible = dossier / f"mcp-{linux_user}{suffixe}.json"
    tmp = dossier / f".mcp-{linux_user}{suffixe}.{os.getpid()}.tmp"
    descripteur = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descripteur, "w", encoding="utf-8") as fh:
        json.dump(contenu, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cible)
    return cible


def environnement_claude(base: dict[str, str] | None = None, *, cle_api: bool = False) -> dict[str, str]:
    """Environnement du processus ``claude`` : auto-update coupé, proxy filtrant si activé, clé API retirée
    en mode abonnement (elle prendrait le pas sur la session OAuth de la personne)."""
    env = dict(os.environ if base is None else base)
    env.setdefault("LANG", "fr_FR.UTF-8")
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    for cle, valeur in proxy_sortant().items():
        env.setdefault(cle, valeur)
    if not cle_api:
        env.pop("ANTHROPIC_API_KEY", None)
    return env


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------
class ClaudeRun:
    """Une exécution de ``claude -p``. Itérer :meth:`deltas` puis lire :attr:`result`."""

    def __init__(
        self,
        cmd: list[str],
        cwd: Path,
        prenom: str,
        timeout: float = TIMEOUT_S,
        cle_api: bool = False,
        linux_user: str | None = None,
        canal: str = "telegram",
    ) -> None:
        self.cmd = cmd
        self.linux_user = linux_user
        self.cwd = cwd
        self.prenom = prenom
        self.timeout = timeout
        self.cle_api = cle_api
        self.canal = canal
        self.result: ClaudeResult | None = None
        self._accum: list[str] = []
        self._saw_delta = False

    async def deltas(self) -> AsyncIterator[str]:
        """Morceaux de texte seulement (canal Telegram, comportement d'avant le lot 4a)."""
        async for ev in self.evenements():
            if ev.kind == "texte":
                yield ev.texte

    async def evenements(self) -> AsyncIterator[EvenementClaude]:
        """Texte, appels d'outils et leurs résultats, dans l'ordre du flux ; :attr:`result` rempli à la fin."""
        env = environnement_claude(cle_api=self.cle_api)
        if self.canal == "web":
            # Mode stdio ou développement : le serveur d'outils lancé par claude hérite de cette variable.
            env["HOMEOS_CANAL"] = "web"
        if self.cle_api and self.linux_user:
            # Personne en clé API : claude (et ses outils) tournent sous homeos ; le bot, qui a authentifié
            # l'expéditeur Telegram, dit aux outils qui parle (rôle propriétaire de l'administration, lot 7a).
            env["HOMEOS_PERSONNE"] = self.linux_user
        if self.cle_api and not env.get("ANTHROPIC_API_KEY"):
            raise ClaudeNotConnected(
                f"Aucune clé API configurée pour {self.prenom} (ANTHROPIC_API_KEY absente de /etc/homeos/anthropic.env).",
                "mode cle_api sans ANTHROPIC_API_KEY",
            )
        try:
            self.cwd.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # créé par install.sh ; l'erreur éventuelle remontera au lancement
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                cwd=str(self.cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=LIMITE_LIGNE,  # une ligne stream-json peut porter une image (lecture d'une photo)
            )
        except FileNotFoundError as exc:
            raise ClaudeMissing(
                "Claude Code n'est pas installé sur le serveur (commande `claude` introuvable).",
                str(exc),
            ) from exc

        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        session_id: str | None = None
        result_event: StreamEvent | None = None
        tool_calls = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout

        try:
            while True:
                restant = deadline - loop.time()
                if restant <= 0:
                    raise TimeoutError
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=restant)
                if not raw:
                    break
                ev = parse_line(raw.decode("utf-8", errors="replace"))
                if ev is None:
                    continue
                if ev.session_id:
                    session_id = ev.session_id
                if ev.kind == "text_delta":
                    self._saw_delta = True
                    self._accum.append(ev.text)
                    yield EvenementClaude("texte", texte=ev.text)
                elif ev.kind == "assistant_text" and not self._saw_delta:
                    self._accum.append(ev.text)
                    yield EvenementClaude("texte", texte=ev.text)
                elif ev.kind == "result":
                    result_event = ev
                for bloc in blocs_outils(ev.raw):
                    tool_calls += 1
                    entree = bloc.get("input")
                    yield EvenementClaude(
                        "outil",
                        outil=str(bloc.get("name") or ""),
                        outil_id=str(bloc.get("id") or "") or None,
                        entree=entree if isinstance(entree, dict) else {},
                    )
                for resultat in blocs_resultats(ev.raw):
                    yield resultat
            await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            await _tuer(proc)
            stderr_task.cancel()
            raise ClaudeTimeout(
                f"Claude n'a pas répondu en {int(self.timeout)} s, j'ai interrompu la demande. "
                "Réessaie, ou envoie /nouveau pour repartir d'une session vide."
            )
        except asyncio.CancelledError:
            await _tuer(proc)
            stderr_task.cancel()
            raise

        try:
            stderr = (await asyncio.wait_for(stderr_task, timeout=5)).decode("utf-8", errors="replace")
        except (TimeoutError, asyncio.CancelledError):
            stderr = ""
        rc = proc.returncode or 0

        texte_final = (result_event.text if result_event and result_event.text else "").strip() or "".join(
            self._accum
        ).strip()
        combine = f"{stderr}\n{texte_final if result_event and result_event.is_error else ''}"

        if (
            rc != 0
            or (result_event is not None and result_event.is_error)
            or (result_event is None and not texte_final)
        ):
            if _MISSING_PATTERNS.search(stderr) and "claude" in stderr.lower():
                raise ClaudeMissing(
                    "Claude Code n'est pas installé pour cet utilisateur (commande `claude` introuvable).",
                    stderr[-500:],
                )
            if "sudo:" in stderr and ("password" in stderr or "mot de passe" in stderr or "not allowed" in stderr):
                raise ClaudeError(
                    "Le bot n'a pas le droit de lancer Claude pour cet utilisateur (règle sudoers manquante).",
                    stderr[-500:],
                )
            if "homeos-claude: refus" in stderr:
                raise ClaudeError(
                    "La commande Claude a été refusée par le garde-fou du serveur (option non autorisée).",
                    stderr[-500:],
                )
            if est_erreur_auth(combine):
                raise ClaudeNotConnected(
                    f"Claude n'est pas connecté pour {self.prenom}, lance `claude auth login` "
                    f"sur le serveur avec son compte Linux.",
                    stderr[-500:],
                )
            if result_event is not None and result_event.is_error:
                detail = texte_final[:300] if texte_final else (result_event.subtype or "erreur")
                raise ClaudeError(f"Claude a renvoyé une erreur : {detail}", stderr[-500:])
            if rc != 0:
                raise ClaudeError(
                    f"Claude s'est arrêté avec le code {rc}. Réessaie dans un instant.",
                    stderr[-500:],
                )
            raise ClaudeError("Claude n'a rien répondu. Réessaie dans un instant.", stderr[-500:])

        mesures = mesures_resultat(result_event.raw) if result_event else {}
        self.result = ClaudeResult(
            text=texte_final,
            session_id=session_id,
            is_error=False,
            subtype=result_event.subtype if result_event else None,
            tool_calls=tool_calls,
            **mesures,
        )


async def _tuer(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (ProcessLookupError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


class ClaudeRunner:
    def __init__(self, cfg: Config, timeout: float = TIMEOUT_S) -> None:
        self.cfg = cfg
        self.timeout = timeout
        # Sans sudo (dev local), on résout le chemin complet ; avec sudo, le PATH de la cible fait foi.
        self.claude_bin = (shutil.which("claude") or "claude") if cfg.no_sudo else "claude"

    def run(
        self,
        linux_user: str,
        prenom: str,
        prompt: str,
        session_id: str | None,
        *,
        cle_api: bool = False,
        canal: str = "telegram",
        modele: str | None = None,
    ) -> ClaudeRun:
        # Prompt système reconstruit à chaque message : une capacité activée ou désactivée est prise en compte
        # sans redémarrer le bot (quelques petits fichiers lus, écriture atomique).
        try:
            preparer_prompt_systeme(self.cfg, linux_user)
        except OSError:
            log.warning("Prompt système non régénéré, version précédente utilisée", exc_info=True)
        mcp_config = None
        if cle_api and self.cfg.mode_outils == "http":
            mcp_config = config_mcp_http(self.cfg, linux_user, canal)
        cmd = construire_commande(
            self.cfg,
            linux_user,
            prompt,
            session_id,
            claude_bin=self.claude_bin,
            cle_api=cle_api,
            mcp_config=mcp_config,
            canal=canal,
            modele=modele,
        )
        log.info(
            "claude -p pour %s (%s, canal %s, modèle %s, resume=%s, %d car.)",
            prenom,
            "clé API" if cle_api else f"user={linux_user}",
            canal,
            modele or "défaut",
            "oui" if session_id else "non",
            len(prompt),
        )
        return ClaudeRun(
            cmd,
            cwd=self.cfg.dossier_travail(linux_user),
            prenom=prenom,
            timeout=self.timeout,
            cle_api=cle_api,
            linux_user=linux_user,
            canal=canal,
        )

    async def auth_status(self, linux_user: str, timeout: float = 20) -> tuple[bool, str]:
        """``sudo -u X claude auth status`` → (connecté ?, résumé court sans secret)."""
        cmd: list[str] = [] if self.cfg.no_sudo else ["sudo", "-n", "-H", "-u", linux_user]
        cmd += [self.claude_bin, "auth", "status"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd="/",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environnement_claude(),
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except FileNotFoundError:
            return False, "claude introuvable"
        except TimeoutError:
            return False, "pas de réponse (timeout)"
        texte = (out.decode("utf-8", "replace") + "\n" + err.decode("utf-8", "replace")).strip()
        connecte = proc.returncode == 0 and not est_erreur_auth(texte)
        # On ne renvoie jamais un token : on garde seulement une ligne courte et neutre.
        resume = _resume_auth(texte, connecte)
        return connecte, resume


def _resume_auth(texte: str, connecte: bool) -> str:
    # Sortie possible en JSON ({"loggedIn": true, "authMethod": "claude.ai", "email": ...})
    try:
        obj = json.loads(texte)
        if isinstance(obj, dict):
            if "loggedIn" in obj and not obj["loggedIn"]:
                return "non connecté"
            methode = obj.get("authMethod") or obj.get("method") or ""
            return f"connecté ({methode})" if methode else "connecté"
    except json.JSONDecodeError:
        pass
    if connecte:
        premiere = next((l.strip() for l in texte.splitlines() if l.strip()), "")
        return "connecté" + (f" — {premiere[:80]}" if premiere and "token" not in premiere.lower() else "")
    return "non connecté"

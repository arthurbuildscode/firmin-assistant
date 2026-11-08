"""Commandes Telegram (§5) : /start /nouveau /profil /journal /stop /reprendre /statut /brief /courses /aide,
/admin (lot 7a), /capacites et les commandes déclarées par les capacités actives (lot 5a, `commandes_telegram` des manifestes),
/identifiants (lot 5b, coffre)."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import db as dbmod
from bot import formatting
from bot.claude_runner import environnement_claude
from bot.config import Config, Personne
from bot.context import BotContext, get_ctx
from bot.handlers import (
    _envoyer,
    envoyer_long,
    est_groupe,
    nettoyer_mention,
    repondre_via_claude,
    verifier_acces,
)

log = logging.getLogger(__name__)

BRIEF_TIMEOUT_S = 240

COMMANDES: list[tuple[str, str]] = [
    ("start", "Accueil (et onboarding si besoin)"),
    ("nouveau", "Repartir d'une conversation vide"),
    ("profil", "Ce que l'assistant sait du foyer et de toi"),
    ("journal", "Les 20 dernières actions"),
    ("courses", "Liste de courses en cours"),
    ("brief", "Brief du jour, maintenant"),
    ("statut", "État du système"),
    ("capacites", "Ce que je sais faire en plus (capacités)"),
    ("admin", "État du système (propriétaires)"),
    ("identifiants", "Où saisir les identifiants d'un service"),
    ("stop", "Mettre en pause rappels et validations"),
    ("reprendre", "Reprendre après une pause"),
    ("aide", "Liste des commandes"),
]


def commandes_capacites(cfg: Config | None) -> list[tuple[str, str, str]]:
    """(commande, aide, capacité) des capacités chargées ; jamais une commande de base du bot."""
    if cfg is None:
        return []
    try:
        from tools import capacites

        actives = capacites.commandes_actives(racine=cfg.capabilities_dir, donnees=cfg.data_dir)
    except Exception:
        log.warning("Commandes des capacités indisponibles", exc_info=True)
        return []
    reservees = {c for c, _ in COMMANDES} | {"help"}
    return [(c.nom, c.aide, cap.nom) for c, cap in actives if c.nom not in reservees]


def commandes_telegram(cfg: Config | None = None) -> list[BotCommand]:
    return [BotCommand(c, d) for c, d in COMMANDES] + [BotCommand(c, d[:256]) for c, d, _ in commandes_capacites(cfg)]


def texte_aide(nom: str, cfg: Config | None = None) -> str:
    lignes = [f"<b>{html.escape(nom)}</b> — l'assistant du foyer", ""]
    lignes += [f"/{c} — {html.escape(d)}" for c, d in COMMANDES]
    lignes += [f"/{c} — {html.escape(d)}" for c, d, _ in commandes_capacites(cfg)]
    lignes += ["", "Sinon, écris-moi ou envoie un vocal, en privé ou dans le groupe."]
    return "\n".join(lignes)


def _acces(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[BotContext, Personne] | None:
    ctx = get_ctx(context)
    personne = verifier_acces(ctx, update)
    if personne is None:
        return None
    return ctx, personne


# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    nom = ctx.cfg.assistant_nom
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/start")
    await _envoyer(
        update,
        f"Salut {html.escape(personne.prenom)} ! Je suis <b>{html.escape(nom)}</b>, l'assistant du foyer.\n"
        "Parle-moi normalement (texte ou vocal). /aide pour les commandes.",
    )
    if not onboarding_fait(ctx.cfg):
        await repondre_via_claude(
            update,
            context,
            personne,
            "/start — l'onboarding n'est pas encore fait. Lance la séance 1 « identité du foyer » "
            "(content/onboarding/00-identite-foyer.md) en te présentant brièvement et en posant la première question.",
        )


def onboarding_fait(cfg: Config, module: str = "00-identite-foyer") -> bool:
    chemin = cfg.data_dir / "onboarding" / f"{module}.json"
    if not chemin.exists():
        return False
    try:
        return bool(json.loads(chemin.read_text(encoding="utf-8")).get("done"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
async def cmd_nouveau(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    # Lot 4a : session Claude vide et nouvelle conversation, visible aussi dans l'appli web.
    ctx.coeur.nouvelle_conversation(personne)
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/nouveau")
    await _envoyer(update, "Nouvelle conversation : je repars de zéro (ta mémoire du foyer est conservée).", html=False)


# ---------------------------------------------------------------------------
def resume_markdown(chemin: Path, max_lignes: int = 6, max_car: int = 350) -> str:
    """Titre + premières lignes non vides d'un fichier Markdown, sans HTML."""
    try:
        texte = chemin.read_text(encoding="utf-8")
    except OSError:
        return ""
    lignes = [l.strip() for l in texte.splitlines() if l.strip()]
    if not lignes:
        return ""
    extrait = " ".join(lignes[:max_lignes])
    if len(extrait) > max_car:
        extrait = extrait[: max_car - 1] + "…"
    return extrait


def construire_profil(cfg: Config, personne: Personne) -> str:
    sections: list[str] = []
    foyer = cfg.data_dir / "foyer"
    for f in sorted(foyer.glob("*.md")) if foyer.exists() else []:
        extrait = resume_markdown(f)
        if extrait:
            sections.append(f"<b>{html.escape(f.stem)}</b>\n{html.escape(extrait)}")
    moi = cfg.data_dir / "personnes" / f"{personne.linux_user}.md"
    extrait = resume_markdown(moi, max_lignes=10, max_car=600)
    if extrait:
        sections.append(f"<b>Toi ({html.escape(personne.prenom)})</b>\n{html.escape(extrait)}")
    if not sections:
        return "Je ne sais encore rien du foyer : lance /start pour commencer l'onboarding."
    return "\n\n".join(sections)


async def cmd_profil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/profil")
    await envoyer_long(update, await asyncio.to_thread(construire_profil, ctx.cfg, personne))


# ---------------------------------------------------------------------------
def formater_journal(rows) -> str:
    if not rows:
        return "Journal vide."
    lignes = ["<b>Journal (20 dernières actions)</b>"]
    for r in rows:
        ts = str(r["ts"] or "")[:16].replace("T", " ")
        who = r["who"] or "—"
        statut = f" [{r['status']}]" if r["status"] and r["status"] not in ("ok", None) else ""
        resume = (r["summary"] or "")[:120]
        lignes.append(f"• {ts} · {html.escape(str(who))} · {html.escape(r['kind'])}{html.escape(statut)} — {html.escape(resume)}")
    return "\n".join(lignes)


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    rows = await dbmod.run(ctx.db.dernieres_actions, 20)
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/journal")
    await envoyer_long(update, formater_journal(rows))


# ---------------------------------------------------------------------------
async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    ctx.cfg.pause_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.cfg.pause_path.write_text(
        f"pause demandée par {personne.prenom} le {datetime.now().astimezone().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/stop (pause)")
    await _envoyer(update, "⏸ Pause : rappels et validations sont suspendus. /reprendre pour réactiver.", html=False)


async def cmd_reprendre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    etait = ctx.cfg.pause_path.exists()
    ctx.cfg.pause_path.unlink(missing_ok=True)
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/reprendre")
    await _envoyer(update, "▶️ C'est reparti." if etait else "Je n'étais pas en pause.", html=False)


# ---------------------------------------------------------------------------
def _derniere_sauvegarde(cfg: Config) -> str:
    candidats = [cfg.data_dir / ".derniere-sauvegarde", Path("/var/backups/homeos/.last-backup"), Path("/var/backups/homeos")]
    for c in candidats:
        if c.exists():
            try:
                mtime = datetime.fromtimestamp(c.stat().st_mtime).astimezone()
                return mtime.strftime("%d/%m %H:%M")
            except OSError:
                continue
    return "inconnue"


def _derniere_restauration(cfg: Config) -> str:
    """Résultat du test de restauration mensuel (scripts/restaurer-test.sh → data/.derniere-restauration)."""
    chemin = cfg.data_dir / ".derniere-restauration"
    try:
        info = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "jamais testée"
    except (OSError, json.JSONDecodeError):
        return "illisible"
    if not isinstance(info, dict):
        return "illisible"
    quand = str(info.get("date") or "")[:16].replace("T", " ")
    if info.get("ok"):
        return f"✅ {quand} ({info.get('depot', '?')}, {info.get('fichiers', '?')} fichiers)"
    return f"❌ {quand} : {str(info.get('message') or 'échec')[:120]}"


def _copie_hors_site(cfg: Config) -> str:
    chemin = cfg.data_dir / ".derniere-copie-distante"
    try:
        return datetime.fromtimestamp(chemin.stat().st_mtime).astimezone().strftime("%d/%m %H:%M")
    except OSError:
        return "non configurée"


def _mesures(cfg: Config, db: dbmod.Db) -> str:
    debut = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    m = db.mesures_mois(debut.isoformat(timespec="seconds"))
    latence = f"{m['latence_mediane_ms'] / 1000:.1f} s" if m["latence_mediane_ms"] is not None else "—"
    return f"{m['cout_usd']:.2f} $ estimés, {m['reponses']} réponses, latence médiane {latence}"


def _espace_disque(cfg: Config) -> str:
    try:
        u = shutil.disk_usage(cfg.data_dir)
    except OSError:
        return "inconnu"
    pct = round(u.used / u.total * 100) if u.total else 0
    return f"{pct} % utilisé ({u.free / 1e9:.1f} Go libres)"


async def cmd_statut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    msg = update.effective_message
    assert msg is not None
    try:
        await msg.chat.send_action(ChatAction.TYPING)
    except TelegramError:
        pass

    abonnes = [p for p in ctx.cfg.personnes if not p.cle_api]
    resultats = await asyncio.gather(*(ctx.runner.auth_status(p.linux_user) for p in abonnes), return_exceptions=True)
    lignes = ["<b>Statut</b>"]
    for p in ctx.cfg.personnes:
        if p.cle_api:
            ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
            lignes.append(f"• Claude ({html.escape(p.prenom)}) : {'✅ clé API' if ok else '❌ clé API absente (/etc/homeos/anthropic.env)'}")
    for p, r in zip(abonnes, resultats):
        if isinstance(r, BaseException):
            lignes.append(f"• Claude ({html.escape(p.prenom)}) : ⚠️ erreur ({html.escape(type(r).__name__)})")
        else:
            ok, resume = r
            lignes.append(f"• Claude ({html.escape(p.prenom)}) : {'✅' if ok else '❌'} {html.escape(resume)}")

    brief = await dbmod.run(ctx.db.dernier_brief)
    if brief is not None:
        lignes.append(f"• Dernier brief : {html.escape(str(brief['ts'])[:16].replace('T', ' '))} ({html.escape(brief['kind'])})")
    else:
        lignes.append("• Dernier brief : aucun")
    lignes.append(f"• Disque : {html.escape(_espace_disque(ctx.cfg))}")
    lignes.append(f"• Dernière sauvegarde : {html.escape(_derniere_sauvegarde(ctx.cfg))}")
    lignes.append(f"• Copie hors site : {html.escape(_copie_hors_site(ctx.cfg))}")
    lignes.append(f"• Restauration testée : {html.escape(_derniere_restauration(ctx.cfg))}")
    try:
        lignes.append(f"• Ce mois-ci : {html.escape(await dbmod.run(_mesures, ctx.cfg, ctx.db))}")
    except Exception:
        log.warning("Mesures du mois indisponibles", exc_info=True)
    lignes.append(f"• Pause : {'⏸ oui' if ctx.cfg.pause_path.exists() else 'non'}")
    actifs = sum(1 for p in ctx.cfg.personnes if ctx.sessions.get(p.telegram_id))
    lignes.append(f"• Sessions de conversation actives : {actifs}/{len(ctx.cfg.personnes)}")
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/statut")
    await _envoyer(update, "\n".join(lignes))


# ---------------------------------------------------------------------------
async def lancer_brief(cfg: Config, quel: str = "jour") -> tuple[bool, str]:
    cmd = [cfg.python_bin, "-m", "tools.runner", "brief", quel]
    env = {"HOMEOS_DATA": str(cfg.data_dir), "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/node22/bin"}
    for k in ("HOME", "LANG", "HOMEOS_ETAT", "HOMEOS_SECRETS", "HOMEOS_ROOT", "PLAYWRIGHT_BROWSERS_PATH"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    cle = cfg.secrets.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if cle:
        env["ANTHROPIC_API_KEY"] = cle
    env = environnement_claude(env, cle_api=True)  # DISABLE_AUTOUPDATER, proxy filtrant
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cfg.root_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=BRIEF_TIMEOUT_S)
    except FileNotFoundError:
        return False, "Le runner des outils est introuvable (tools/runner.py)."
    except TimeoutError:
        return False, "Le brief a mis trop de temps à se générer."
    sortie = out.decode("utf-8", "replace").strip()
    erreur = err.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        log.warning("runner brief %s : code %s, stderr=%s", quel, proc.returncode, erreur[-500:])
        return False, (erreur.splitlines()[-1][:300] if erreur else "échec sans message")
    return True, sortie or "(brief vide)"


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    if ctx.cfg.pause_path.exists():
        await _envoyer(update, "Le foyer est en pause (/reprendre pour réactiver).", html=False)
        return
    attente = await _envoyer(update, "Je prépare le brief du jour…", html=False)
    ok, texte = await lancer_brief(ctx.cfg, "jour")
    await dbmod.run(ctx.db.log, personne.prenom, "brief_jour", "/brief à la demande", None, dbmod.STATUS_OK if ok else dbmod.STATUS_ERROR, texte[:500])
    try:
        await attente.delete()
    except TelegramError:
        pass
    if ok:
        await envoyer_long(update, formatting.md_vers_html(texte))
    else:
        await _envoyer(update, f"⚠️ Brief impossible : {texte}", html=False)


# ---------------------------------------------------------------------------
def formater_courses(rows) -> str:
    if rows is None:
        return "La liste de courses n'existe pas encore : dis-moi simplement « ajoute du lait aux courses »."
    if not rows:
        return "🛒 La liste de courses est vide."
    lignes = ["<b>🛒 Courses</b>"]
    rayon_courant = object()
    for r in rows:
        rayon = r["rayon"] or "Divers"
        if rayon != rayon_courant:
            rayon_courant = rayon
            lignes.append(f"\n<i>{html.escape(str(rayon))}</i>")
        lignes.append(f"• {html.escape(str(r['article']))}")
    return "\n".join(lignes)


async def cmd_courses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    rows = await dbmod.run(ctx.db.courses)
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/courses")
    await envoyer_long(update, formater_courses(rows))


# ---------------------------------------------------------------------------
async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, _personne = acc
    await _envoyer(update, texte_aide(ctx.cfg.assistant_nom, ctx.cfg))


# ---------------------------------------------------------------------------
# Capacités (lot 5a)
# ---------------------------------------------------------------------------
STATUTS_LISIBLES = {
    "actif": "✅ active",
    "essai": "🧪 en essai (consultation seulement)",
    "inactif": "⏸ désactivée",
    "invalide": "⚠️ à corriger",
}


def formater_capacites(infos: list[dict], personnes_cfg: list[Personne] | None = None) -> str:
    """Liste lisible des capacités pour /capacites (HTML Telegram, sans jargon)."""
    if not infos:
        return "Je n'ai pas encore de capacité en plus des outils de base (agenda, courses, échéances, mémoire…)."
    prenoms = {p.linux_user: p.prenom for p in personnes_cfg or []}
    lignes = ["<b>Ce que je sais faire en plus</b>"]
    for i in infos:
        statut = STATUTS_LISIBLES.get(i.get("statut", ""), i.get("statut", ""))
        lignes.append(f"\n• <b>{html.escape(str(i.get('titre')))}</b> — {html.escape(statut)}")
        if i.get("description"):
            lignes.append(f"  {html.escape(str(i['description']))}")
        if i.get("commandes"):
            lignes.append(f"  Commande : {html.escape(' '.join(i['commandes']))}")
        if i.get("personnes"):
            qui = ", ".join(prenoms.get(p, p.capitalize()) for p in i["personnes"])
            lignes.append(f"  Pour : {html.escape(qui)}")
        smoke = i.get("dernier_smoke") or {}
        if smoke and i.get("statut") in ("actif", "essai"):
            quand = str(smoke.get("quand") or "")
            quand = f"{quand[8:10]}/{quand[5:7]} {quand[11:16]}" if len(quand) >= 16 else ""
            etat = "tout va bien" if smoke.get("ok") else f"problème — {smoke.get('message', '')}"
            lignes.append(f"  Dernière vérification {html.escape(quand)} : {html.escape(etat)}")
    return "\n".join(lignes)


async def cmd_capacites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc

    def lister() -> list[dict]:
        from tools import capacites

        return capacites.lister(racine=ctx.cfg.capabilities_dir, donnees=ctx.cfg.data_dir)

    try:
        infos = await asyncio.to_thread(lister)
    except Exception:
        log.exception("Liste des capacités impossible")
        await _envoyer(update, "⚠️ Je n'arrive pas à lire la liste des capacités pour l'instant.", html=False)
        return
    await dbmod.run(ctx.db.log, personne.prenom, "commande", "/capacites")
    await envoyer_long(update, formater_capacites(infos, ctx.cfg.personnes))


def analyser_commande(texte: str) -> tuple[str, str | None]:
    """« /bus@MonBot <ligne> » → ("bus", "MonBot")."""
    premier = (texte or "").split(maxsplit=1)[0] if (texte or "").strip() else ""
    commande, _, cible = premier.lstrip("/").partition("@")
    return commande.lower(), (cible or None)


async def cmd_capacite_dynamique(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Commande non reconnue par les handlers de base : si une capacité chargée la déclare, le texte est transmis
    tel quel à l'assistant (« /bus … ») ; aucune logique métier côté bot."""
    msg = update.effective_message
    if msg is None or not msg.text:
        return
    commande, cible = analyser_commande(msg.text)
    nom_bot = context.bot.username if context.bot else None
    if cible and nom_bot and cible.lower() != nom_bot.lower():
        return  # commande adressée à un autre bot du groupe
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    trouvee = next((t for t in commandes_capacites(ctx.cfg) if t[0] == commande), None)
    if trouvee is None:
        if not est_groupe(update) or cible:
            await _envoyer(update, "Commande inconnue. /aide pour la liste.", html=False)
        return

    def autorisee() -> bool:
        from tools import capacites

        cap = capacites.trouver(trouvee[2], racine=ctx.cfg.capabilities_dir)
        return cap.manifeste is not None and capacites.personne_autorisee(cap.manifeste, personne.linux_user, ctx.cfg.raw)

    if not await asyncio.to_thread(autorisee):
        await _envoyer(update, "Cette commande n'est pas ouverte pour toi.", html=False)
        return
    await dbmod.run(ctx.db.log, personne.prenom, "commande", f"/{commande} (capacité {trouvee[2]})")
    await repondre_via_claude(update, context, personne, nettoyer_mention(msg.text, nom_bot))


# ---------------------------------------------------------------------------
# /admin (lot 7a) : état du système, réservé aux propriétaires ; les réglages se font en conversation.
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    if not personne.proprietaire:
        await dbmod.run(ctx.db.log, personne.prenom, "commande", "/admin (refusé : rôle)", None, "ignored")
        await _envoyer(update, "Ce réglage est réservé aux propriétaires du foyer.", html=False)
        return

    def etat() -> str:
        from tools import administration

        return administration.etat_texte(
            administration.etat(racine=ctx.cfg.capabilities_dir, donnees=ctx.cfg.data_dir)
        )

    try:
        texte = await asyncio.to_thread(etat)
    except Exception:
        log.exception("État du système indisponible")
        await _envoyer(update, "⚠️ Je n'arrive pas à lire l'état du système pour l'instant.", html=False)
        return
    await dbmod.run(ctx.db.log, personne.prenom, "admin_etat", "/admin")
    texte += "\n\nPour changer un réglage, dis-le-moi simplement (« mets le brief du matin à 7h30 »)."
    await envoyer_long(update, html.escape(texte))


# ---------------------------------------------------------------------------
# /identifiants (lot 5b) : jamais de saisie dans Telegram, seulement la marche à suivre.
RE_CAPACITE = re.compile(r"^[a-z0-9_]{1,40}$")


def _nom_capacite(texte: str) -> str:
    """« Planity », « cantine-école » → « planity », « cantine_ecole »."""
    t = unicodedata.normalize("NFKD", texte.strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[-\s]+", "_", t)


def analyser_identifiants(args: list[str] | None) -> tuple[str, str | None]:
    """Classe les arguments de /identifiants : ("aide", None) | ("capacite", nom) | ("refus", None).

    Plus d'un argument, ou un argument qui n'a pas la forme d'un nom de capacité, ressemble à un
    identifiant collé dans Telegram : on le refuse (et le message est effacé si possible).
    """
    if not args:
        return "aide", None
    if len(args) > 1:
        return "refus", None
    nom = _nom_capacite(args[0])
    if not RE_CAPACITE.match(nom):
        return "refus", None
    return "capacite", nom


def commande_coffre(cfg: Config, cle: str) -> str:
    """Saisie sur le serveur (lot 5c : wrapper `homeos-coffre`, installé par install.sh dans /usr/local/bin)."""
    return f"sudo -u homeos homeos-coffre set {cle}"


def texte_identifiants(cfg: Config, personne: Personne, capacite: str | None) -> str:
    if capacite is None:
        return (
            "🔐 <b>Identifiants</b>\n"
            "Usage : /identifiants &lt;service&gt; (ex. /identifiants planity).\n"
            "Je t'indique où les saisir : jamais dans Telegram.\n"
            "Ou demande-moi simplement « quels identifiants sont enregistrés ? » : je liste les entrées du coffre "
            "(noms, types, ce qui manque), sans jamais montrer une valeur."
        )
    cle = f"{capacite}/{personne.linux_user}"
    titre = capacite.replace("_", " ").capitalize()
    return (
        f"🔐 <b>Identifiants {html.escape(titre)}</b>\n"
        "Pour ta sécurité, les identifiants se saisissent sur le serveur (ou dans l'appli web à venir), "
        "jamais ici :\n"
        f"<code>{html.escape(commande_coffre(cfg, cle))}</code>\n"
        f"(clé <code>{html.escape(capacite)}</code> seule si le compte est commun au foyer.)\n"
        "La saisie est masquée et chiffrée dans le coffre ; je n'utilise qu'une référence, jamais les valeurs.\n"
        "En conversation : « quels identifiants sont enregistrés ? » (liste sans aucune valeur) ou "
        "« prépare les identifiants de … » (je crée l'entrée et je te redonne cette marche à suivre)."
    )


def texte_refus_identifiants(efface: bool) -> str:
    return (
        "🔐 Je n'enregistre jamais un identifiant ou un mot de passe envoyé dans Telegram"
        + (" et j'ai effacé ton message." if efface else " (je n'ai pas pu effacer ton message : supprime-le toi-même).")
        + "\nPar précaution, change ce mot de passe, puis saisis le nouveau sur le serveur : "
        "/identifiants &lt;service&gt; pour la marche à suivre."
    )


async def cmd_identifiants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    acc = _acces(update, context)
    if acc is None:
        return
    ctx, personne = acc
    genre, capacite = analyser_identifiants(list(context.args or []))
    if genre == "refus":
        efface = False
        msg = update.effective_message
        if msg is not None:
            try:
                await msg.delete()
                efface = True
            except TelegramError:
                efface = False
        # Jamais le texte reçu dans le journal : il contient peut-être un mot de passe.
        await dbmod.run(ctx.db.log, personne.prenom, "commande", "/identifiants (valeur refusée" + (", message effacé)" if efface else ")"))
        chat = update.effective_chat
        if efface and chat is not None:
            # Le message d'origine n'existe plus : pas de réponse citée, un simple envoi dans le chat.
            await context.bot.send_message(chat.id, texte_refus_identifiants(efface), parse_mode=ParseMode.HTML)
        else:
            await _envoyer(update, texte_refus_identifiants(efface))
        return
    await dbmod.run(ctx.db.log, personne.prenom, "commande", f"/identifiants {capacite or ''}".strip())
    await _envoyer(update, texte_identifiants(ctx.cfg, personne, capacite))

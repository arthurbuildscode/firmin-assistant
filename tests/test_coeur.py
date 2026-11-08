"""Lot 4a — cœur de la conversation : routeur de modèle, renouvellement des sessions Claude, sessions partagées
entre processus, historique. Sans réseau, sans `claude` (faux runner)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bot.config import Config, parse_config
from bot.db import Db
from bot.sessions import Sessions
from coeur import routeur
from coeur.claude import ClaudeResult, EvenementClaude, construire_commande
from coeur.conversation import Coeur, bloc_contexte_recent
from coeur.historique import Historique
from tools.modeles import ReglagesIA, modele_valide, reglages_ia

CONFIG_RAW: dict[str, Any] = {
    "assistant": {"nom": "Firmin"},
    "personnes": [
        {"telegram_id": 101, "prenom": "Alice", "linux_user": "alice"},
        {"telegram_id": 202, "prenom": "Bruno", "linux_user": "bruno", "role": "membre"},
    ],
}


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.delenv("HOMEOS_ETAT", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "repo"
    (root / "content").mkdir(parents=True)
    (root / "tools").mkdir()
    return parse_config(json.loads(json.dumps(CONFIG_RAW)), data, root)


# ---------------------------------------------------------------------------
# Réglages `ia`
# ---------------------------------------------------------------------------
def test_reglages_ia_defauts_et_valeurs() -> None:
    d = reglages_ia({})
    assert (d.modele_simple, d.modele, d.modele_decisions, d.routeur) == ("haiku", "sonnet", "opus", True)
    assert (d.session_max_reponses, d.session_ttl_heures) == (20, 2.0)
    r = reglages_ia({"ia": {"modele": "claude-sonnet-4-5", "modele_decisions": "Opus", "routeur": False,
                            "session_max_reponses": 5, "session_ttl_heures": 0.5}})
    assert (r.modele, r.modele_decisions, r.routeur, r.session_max_reponses, r.session_ttl_heures) == (
        "claude-sonnet-4-5", "opus", False, 5, 0.5)
    # Valeurs invalides : défauts (jamais d'option arbitraire transmise à claude).
    m = reglages_ia({"ia": {"modele": "sonnet --dangerously-skip-permissions", "session_max_reponses": "x",
                            "session_ttl_heures": -1}})
    assert (m.modele, m.session_max_reponses, m.session_ttl_heures) == ("sonnet", 20, 2.0)
    assert modele_valide("haiku") and modele_valide("claude-opus-4-1") and not modele_valide("gpt-4")
    assert not modele_valide("claude-../x") and not modele_valide("Opus")


def test_commande_avec_modele(cfg: Config) -> None:
    cmd = construire_commande(cfg, "alice", "salut", None, modele="haiku")
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--model" not in construire_commande(cfg, "alice", "salut", None)
    assert "--model" not in construire_commande(cfg, "alice", "salut", None, modele="gpt-4; rm -rf /")


# ---------------------------------------------------------------------------
# Routeur : règles sans IA
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("texte", "attendu"),
    [
        ("/bus", ("simple", "commande")),
        ("/kd semaine prochaine", ("simple", "commande")),
        ("oui", ("simple", "confirmation")),
        ("Ok merci !", ("simple", "confirmation")),
        ("C'est bon", ("simple", "confirmation")),
        ("vas-y", ("simple", "confirmation")),
        ("ajoute du lait et des œufs", ("simple", "action_courte")),
        ("Coche le pain", ("simple", "action_courte")),
        ("peux-tu noter le dentiste jeudi ?", ("simple", "action_courte")),
        ("[vocal transcrit] Ajoute des pommes", ("simple", "action_courte")),
        ("Quand est le prochain bus ?", ("simple", "action_courte")),
        ("Choisis les plats Kitchen Daily de la semaine", ("decision", "decision")),
        ("compare les deux offres d'assurance", ("decision", "decision")),
        ("Décide pour moi entre le resto et la maison", ("decision", "decision")),
        ("fais la sélection de la semaine", ("decision", "decision")),
        ("[fichier reçu: /srv/x.pdf] range-le", ("standard", "fichier")),
    ],
)
def test_routeur_regles(texte: str, attendu: tuple[str, str]) -> None:
    assert routeur.niveau_par_regles(texte) == attendu


def test_routeur_sans_regle() -> None:
    long = "ajoute " + " ".join(["mot"] * 20)  # verbe connu mais message long : pas de règle
    assert routeur.niveau_par_regles(long) is None
    assert routeur.niveau_par_regles("Raconte-moi comment s'est passée la réunion de l'école hier soir") is None


def test_routeur_classification_et_repli() -> None:
    ia = ReglagesIA()

    def classifieur(reponse: Any, attente: float = 0) -> routeur.Classifieur:
        async def classer(texte: str) -> str | None:
            if attente:
                await asyncio.sleep(attente)
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        return classer

    texte = "Raconte-moi ce que tu sais faire pour notre famille, en détail"
    r = asyncio.run(routeur.choisir(texte, ia, classifieur("decision")))
    assert (r.niveau, r.modele, r.raison) == ("decision", "opus", "classification")
    r = asyncio.run(routeur.choisir(texte, ia, classifieur(" Simple.\n")))
    assert (r.niveau, r.modele) == ("simple", "haiku")
    for mauvais in ("je ne sais pas", None, RuntimeError("panne")):
        r = asyncio.run(routeur.choisir(texte, ia, classifieur(mauvais)))
        assert (r.niveau, r.modele, r.raison) == ("standard", "sonnet", "repli")
    # Délai dépassé (3 s + marge) : repli standard sans attendre plus.
    lent = classifieur("decision", attente=10)
    debut = asyncio.run(asyncio.wait_for(routeur.choisir(texte, ia, lent), timeout=6))
    assert debut.raison == "repli"
    # Règle prioritaire : pas d'appel au classifieur.
    appels: list[str] = []

    async def espion(t: str) -> str:
        appels.append(t)
        return "decision"

    assert asyncio.run(routeur.choisir("oui", ia, espion)).modele == "haiku" and appels == []
    # Routeur coupé : toujours le modèle courant.
    inactif = ReglagesIA(routeur=False, modele="claude-sonnet-4-5")
    r = asyncio.run(routeur.choisir("/bus", inactif, espion))
    assert (r.modele, r.raison) == ("claude-sonnet-4-5", "inactif")


def test_routeur_classifieur_claude_sans_cle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans clé API, pas d'appel : None (repli standard)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    classer = routeur.classifieur_claude("haiku", "{}", tmp_path)
    assert asyncio.run(classer("bonjour")) is None


def test_routeur_classifieur_claude_faux_binaire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude --bare -p … --model haiku` : sortie JSON lue, arguments vérifiés, délai respecté."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    trace = tmp_path / "args"
    script = bin_ / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{trace}'\n"
        "printf '%s' '{\"type\":\"result\",\"result\":\"decision\",\"is_error\":false}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    classer = routeur.classifieur_claude("haiku", '{"permissions":{}}', tmp_path / "cwd")
    assert asyncio.run(classer("compare ces offres")) == "decision"
    args = trace.read_text(encoding="utf-8").splitlines()
    assert args[0] == "--bare" and "--model" in args and args[args.index("--model") + 1] == "haiku"
    assert "--max-turns" in args and args[args.index("--max-turns") + 1] == "1"
    lent = bin_ / "claude"
    lent.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    classer = routeur.classifieur_claude("haiku", "{}", tmp_path / "cwd", delai_s=0.5)
    assert asyncio.run(classer("x")) is None


# ---------------------------------------------------------------------------
# Sessions : renouvellement, partage entre processus
# ---------------------------------------------------------------------------
def test_sessions_compteur_et_renouvellement(tmp_path: Path) -> None:
    s = Sessions(tmp_path / "sessions.json")
    for _ in range(3):
        s.set(1, "alice", "sess-a")
    assert s.reponses(1) == 3
    assert s.get(1, max_reponses=4) == "sess-a"
    assert s.get(1, max_reponses=3) is None  # 3 réponses déjà données : nouvelle session
    s.set(1, "alice", "sess-b")  # nouvelle session : compteur remis à 1
    assert s.reponses(1) == 1
    plus_tard = datetime.now().astimezone() + timedelta(hours=3)
    assert s.get(1, now=plus_tard, ttl=timedelta(hours=2)) is None
    assert s.get(1, now=plus_tard) == "sess-b"  # TTL historique (12 h) par défaut


def test_sessions_partagees_entre_processus(tmp_path: Path) -> None:
    """Deux instances (bot, appli) : chacune relit le fichier modifié par l'autre et n'écrase pas son entrée."""
    bot = Sessions(tmp_path / "sessions.json")
    appli = Sessions(tmp_path / "sessions.json")
    bot.set(1, "alice", "sess-a")
    assert appli.get(1) == "sess-a"
    appli.set(2, "bruno", "sess-b")
    bot.set(1, "alice", "sess-a")
    data = json.loads((tmp_path / "sessions.json").read_text())
    assert data["1"]["reponses"] == 2 and data["2"]["session_id"] == "sess-b"
    appli.reset(1)
    assert bot.get(1) is None and bot.get(2) == "sess-b"


def test_historique_compteur_session_et_messages_avant(tmp_path: Path) -> None:
    h = Historique(tmp_path / "appli.sqlite")
    conv = h.creer("alice")
    h.maj_session(conv["id"], "s1")
    h.maj_session(conv["id"], "s1")
    assert h.conversation(conv["id"])["session_reponses"] == 2
    h.maj_session(conv["id"], "s2")
    assert h.conversation(conv["id"])["session_reponses"] == 1
    ids = [h.ajouter_message(conv["id"], "personne", "web", f"m{i}", auteur="Alice") for i in range(5)]
    avant = h.messages_avant(conv["id"], ids[4], 3)
    assert [m["texte"] for m in avant] == ["m1", "m2", "m3"]


def test_historique_migration_colonne(tmp_path: Path) -> None:
    import sqlite3

    chemin = tmp_path / "appli.sqlite"
    conn = sqlite3.connect(chemin)
    conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, linux_user TEXT NOT NULL, "
                 "titre TEXT NOT NULL DEFAULT '', cree TEXT NOT NULL, maj TEXT NOT NULL, session_id TEXT, "
                 "archivee INTEGER NOT NULL DEFAULT 0)")
    conn.commit()
    conn.close()
    h = Historique(chemin)
    conv = h.creer("alice")
    h.maj_session(conv["id"], "s1")
    assert h.conversation(conv["id"])["session_reponses"] == 1


def test_bloc_contexte_recent() -> None:
    messages = [{"role": "personne", "auteur": "Alice", "texte": f"question {i}\nsuite"} for i in range(7)]
    messages += [{"role": "systeme", "texte": "⚠️ erreur"}, {"role": "assistant", "texte": "x" * 500}]
    bloc = bloc_contexte_recent(messages, "Firmin")
    lignes = bloc.splitlines()
    assert lignes[0].startswith("[contexte récent") and lignes[-1] == "[fin du contexte récent]"
    assert len(lignes) == 2 + 6  # 6 lignes au plus
    assert "Alice : question 1" not in bloc and "Alice : question 2 suite" in bloc
    assert lignes[-2].startswith("Firmin : ") and len(lignes[-2]) <= len("Firmin : ") + 200
    assert "erreur" not in bloc
    assert bloc_contexte_recent([], "Firmin") == ""


# ---------------------------------------------------------------------------
# Cœur : renouvellement de session et modèle de bout en bout (faux runner)
# ---------------------------------------------------------------------------
class FauxRun:
    def __init__(self, session: str, texte: str) -> None:
        self.session = session
        self.texte = texte
        self.result: ClaudeResult | None = None

    async def evenements(self) -> Any:
        yield EvenementClaude("texte", texte=self.texte)
        self.result = ClaudeResult(text=self.texte, session_id=self.session, cout_usd=0.01, duree_ms=12)


class FauxRunner:
    def __init__(self) -> None:
        self.appels: list[dict[str, Any]] = []
        self.compteur = 0

    def run(self, linux_user: str, prenom: str, prompt: str, session_id: str | None, **kw: Any) -> FauxRun:
        self.appels.append({"prompt": prompt, "session_id": session_id, **kw})
        # Session reprise si demandée, sinon nouvelle session.
        if session_id is None:
            self.compteur += 1
        return FauxRun(session_id or f"sess-{self.compteur}", f"réponse {len(self.appels)}")


class Ctx:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Db(cfg.db_path)
        self.db.init()
        self.sessions = Sessions(cfg.sessions_path)
        self.runner = FauxRunner()


def _echanger(coeur: Coeur, personne: Any, texte: str, **kw: Any) -> list[Any]:
    async def go() -> list[Any]:
        return [ev async for ev in coeur.echanger(personne, texte, **kw)]

    return asyncio.run(go())


def test_coeur_renouvelle_la_session_apres_n_reponses(cfg: Config) -> None:
    cfg.raw["ia"] = {"session_max_reponses": 2}
    ctx = Ctx(cfg)

    async def classer(texte: str) -> str:
        return "standard"

    coeur = Coeur(ctx, Historique(cfg.etat_dir / "appli.sqlite"), classifieur=classer)
    alice = cfg.personnes[0]
    for texte in ("ajoute du lait", "ajoute des œufs", "Raconte-moi ce qui est prévu ce week-end en famille"):
        evs = _echanger(coeur, alice, texte, canal="web")
    appels = ctx.runner.appels
    assert [a["session_id"] for a in appels] == [None, "sess-1", None]
    assert "[contexte récent" not in appels[0]["prompt"]  # conversation vierge : rien à rappeler
    assert "[contexte récent" not in appels[1]["prompt"]  # session reprise
    troisieme = appels[2]["prompt"]
    assert troisieme.startswith("[contexte récent")
    assert "Alice : ajoute du lait" in troisieme and "Firmin : réponse 2" in troisieme
    assert "Raconte-moi" not in troisieme.split("[fin du contexte récent]")[0]  # le message courant n'y est pas
    # Modèle : règles (haiku) puis classification (sonnet), consigné dans la fin et dans actions_log.
    assert [a["modele"] for a in appels] == ["haiku", "haiku", "sonnet"]
    fin = next(e for e in evs if e.type == "fin")
    assert fin.donnees["modele"] == "sonnet" and fin.donnees["niveau"] == "standard"
    with ctx.db.connect() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM actions_log WHERE kind = 'message_sortant' ORDER BY id")]
    assert [p["modele"] for p in payloads] == ["haiku", "haiku", "sonnet"]
    assert payloads[2]["nouvelle_session"] is True and payloads[1]["nouvelle_session"] is False
    # L'historique reste complet (affiché par l'appli) même après le renouvellement.
    conv = coeur.historique.courante("alice")
    assert len(coeur.historique.messages(conv["id"])) == 6


def test_coeur_renouvelle_apres_inactivite(cfg: Config) -> None:
    ctx = Ctx(cfg)
    coeur = Coeur(ctx, Historique(cfg.etat_dir / "appli.sqlite"), classifieur=lambda t: asyncio.sleep(0, "simple"))
    alice = cfg.personnes[0]
    _echanger(coeur, alice, "ajoute du lait")
    # Dernière activité il y a 3 h (> ia.session_ttl_heures = 2) : nouvelle session, contexte récent.
    data = json.loads(cfg.sessions_path.read_text())
    data["101"]["updated"] = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    cfg.sessions_path.write_text(json.dumps(data))
    _echanger(coeur, alice, "ajoute du pain")
    assert ctx.runner.appels[1]["session_id"] is None
    assert ctx.runner.appels[1]["prompt"].startswith("[contexte récent")


def test_coeur_routeur_desactive_et_telegram(cfg: Config) -> None:
    cfg.raw["ia"] = {"routeur": False}
    ctx = Ctx(cfg)
    coeur = Coeur(ctx, Historique(cfg.etat_dir / "appli.sqlite"))
    evs = _echanger(coeur, cfg.personnes[1], "Choisis les plats", canal="telegram")
    assert ctx.runner.appels[0]["modele"] == "sonnet" and ctx.runner.appels[0]["canal"] == "telegram"
    assert next(e for e in evs if e.type == "fin").donnees["modele"] == "sonnet"


def test_runner_crons_modele(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tools import runner

    monkeypatch.setenv("HOMEOS_DATA", str(tmp_path))
    (tmp_path / "config.yaml").write_text("ia:\n  modele: sonnet\n  modele_decisions: claude-opus-4-1\n")
    assert runner.modele_cron() == "sonnet"
    assert runner.modele_cron("modele_decisions") == "claude-opus-4-1"
    cmd = runner.commande_cron("claude", "prompt", "claude-opus-4-1")
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-1"
    assert "--model" not in runner.commande_cron("claude", "prompt", "pas un modèle")


# ---------------------------------------------------------------------------
# Contrat des cartes (CONTRACT §14)
# ---------------------------------------------------------------------------
def test_extraction_des_cartes() -> None:
    from coeur import cartes

    bonne = {"type": "courses", "id": "c-1", "donnees": {"rayons": [{"rayon": "Frais", "articles": [
        {"article": "lait"}, {"article": "œufs", "coche": True}]}]}, "actions": [{"libelle": "Ouvrir", "action": "ouvrir"}]}
    texte = ("Voici la liste.\nCARTE:" + json.dumps(bonne) + "\nCARTE:{\"type\":\"inconnu\",\"donnees\":{}}\n"
             "CARTE:{pas du json}\n`CARTE:" + json.dumps({"type": "brief", "donnees": {}}) + "`\nFin.")
    nettoye, trouvees = cartes.extraire_cartes(texte)
    assert "CARTE:" not in nettoye and nettoye.startswith("Voici la liste.") and nettoye.endswith("Fin.")
    assert [c["id"] for c in trouvees] == ["c-1"]  # type inconnu, JSON illisible, champ obligatoire manquant : écartés
    assert trouvees[0]["actions"][0]["args"] == {}
    assert cartes.erreurs_carte({"type": "bus", "donnees": {"passages": []}, "actions": [{"libelle": "x",
                                                                                         "action": "executer"}]})
    assert cartes.rendu_texte(trouvees[0]) == "• Frais : lait"
    identifiants = cartes.carte_identifiants(
        "Identifiants Kitchen Daily à renseigner (champs manquants : mot_de_passe) : /identifiants kitchen_daily")
    assert identifiants is not None and identifiants["donnees"] == {
        "capacite": "kitchen_daily", "titre": "Kitchen Daily", "champs_manquants": ["mot_de_passe"]}
    assert identifiants["actions"][0]["action"] == "coffre"


def test_base_appli_privee(tmp_path: Path) -> None:
    import os
    import stat

    from web.stockage import StockageAuth

    ancien = os.umask(0o002)
    try:
        h = Historique(tmp_path / "appli.sqlite")
        h.creer("alice")
        StockageAuth(tmp_path / "appli.sqlite").handle("alice")
    finally:
        os.umask(ancien)
    for f in tmp_path.glob("appli.sqlite*"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name

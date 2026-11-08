"""Tests du chantier bot — sans Telegram, sans `claude`, sans réseau."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import confirmations, formatting
from bot.claude_runner import (
    ClaudeNotConnected,
    ClaudeRun,
    ClaudeRunner,
    construire_commande,
    environnement_claude,
    est_erreur_auth,
    mesures_resultat,
    parse_line,
    preparer_prompt_systeme,
    substituer_nom,
)
from bot.config import Config, Personne, parse_config, parse_secrets_env
from bot.db import Db
from bot.handlers import ligne_contexte, nettoyer_mention
from bot.sessions import Sessions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
CONFIG_RAW = {
    "assistant": {"nom": "Testbot", "tutoiement": True, "langue": "fr"},
    "personnes": [
        {"telegram_id": 111111111, "prenom": "Alex", "linux_user": "alex", "role": "proprietaire"},
        {"telegram_id": 222222222, "prenom": "Camille", "linux_user": "camille", "role": "proprietaire"},
    ],
    "groupe_telegram_id": -100123456789,
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "repo"
    (root / "content").mkdir(parents=True)
    (root / "tools").mkdir()
    return parse_config(CONFIG_RAW, data, root)


STREAM_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess-123","model":"claude","tools":[]}',
    '{"type":"stream_event","event":{"type":"message_start","message":{"id":"m1"}},"session_id":"sess-123"}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Bonjour "}},"session_id":"sess-123"}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Alex."}},"session_id":"sess-123"}',
    "ceci n'est pas du json",
    "",
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Bonjour Alex."}]},"session_id":"sess-123"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"mcp__homeos__courses_lister","input":{}}]},"session_id":"sess-123"}',
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"[]"}]},"session_id":"sess-123"}',
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":1200,"num_turns":2,"result":"Bonjour Alex.","session_id":"sess-123"}',
]


# ---------------------------------------------------------------------------
# Parsing stream-json
# ---------------------------------------------------------------------------
def test_parse_line_types() -> None:
    events = [parse_line(l) for l in STREAM_LINES]
    kinds = [e.kind for e in events if e is not None]
    assert kinds == ["init", "other", "text_delta", "text_delta", "assistant_text", "tool_use", "other", "result"]
    assert parse_line("ceci n'est pas du json") is None
    assert parse_line("") is None
    deltas = [e.text for e in events if e and e.kind == "text_delta"]
    assert "".join(deltas) == "Bonjour Alex."
    result = next(e for e in events if e and e.kind == "result")
    assert result.session_id == "sess-123"
    assert result.text == "Bonjour Alex."
    assert result.is_error is False


def test_parse_line_result_error() -> None:
    ev = parse_line('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Not logged in · Please run /login","session_id":"s"}')
    assert ev is not None and ev.kind == "result" and ev.is_error
    assert est_erreur_auth(ev.text)
    assert not est_erreur_auth("tout va bien")


def _ecrire_faux_claude(tmp_path: Path, lignes: list[str], code: int = 0, stderr: str = "") -> Path:
    """Crée un script `claude` factice qui rejoue les lignes stream-json."""
    script = tmp_path / "claude"
    body = "\n".join(lignes).replace("'", "'\\''")
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{body}'\n"
        + (f"printf '%s\\n' '{stderr}' >&2\n" if stderr else "")
        + f"exit {code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_claude_run_streaming_et_resultat(tmp_path: Path, cfg: Config) -> None:
    script = _ecrire_faux_claude(tmp_path, STREAM_LINES)
    cmd = [str(script), "-p", "salut"]
    run = ClaudeRun(cmd, cwd=cfg.data_dir, prenom="Alex", timeout=10)

    async def collect() -> list[str]:
        return [d async for d in run.deltas()]

    deltas = asyncio.run(collect())
    # Les deltas priment ; le bloc `assistant` n'est pas re-émis (pas de doublon).
    assert deltas == ["Bonjour ", "Alex."]
    assert run.result is not None
    assert run.result.text == "Bonjour Alex."
    assert run.result.session_id == "sess-123"
    assert run.result.tool_calls == 1


def test_claude_run_sans_deltas_utilise_assistant(tmp_path: Path, cfg: Config) -> None:
    lignes = [STREAM_LINES[0], STREAM_LINES[6], STREAM_LINES[-1]]
    script = _ecrire_faux_claude(tmp_path, lignes)
    run = ClaudeRun([str(script)], cwd=cfg.data_dir, prenom="Alex", timeout=10)

    async def collect() -> list[str]:
        return [d async for d in run.deltas()]

    assert asyncio.run(collect()) == ["Bonjour Alex."]
    assert run.result is not None and run.result.text == "Bonjour Alex."


def test_claude_run_session_expiree(tmp_path: Path, cfg: Config) -> None:
    lignes = ['{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Not logged in. Please run /login","session_id":"s"}']
    script = _ecrire_faux_claude(tmp_path, lignes, code=1)
    run = ClaudeRun([str(script)], cwd=cfg.data_dir, prenom="Camille", timeout=10)

    async def collect() -> None:
        async for _ in run.deltas():
            pass

    with pytest.raises(ClaudeNotConnected) as exc:
        asyncio.run(collect())
    assert "Claude n'est pas connecté pour Camille" in exc.value.message
    assert "claude auth login" in exc.value.message


def test_construire_commande(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMEOS_NO_SUDO", raising=False)
    monkeypatch.delenv("HOMEOS_CLAUDE_WRAPPER", raising=False)
    monkeypatch.delenv("HOMEOS_ETAT", raising=False)
    monkeypatch.setenv("HOMEOS_SECRETS", "/etc/homeos")
    for d in ("foyer", "personnes", "fichiers", "onboarding"):
        (cfg.data_dir / d).mkdir()
    cmd = construire_commande(cfg, "alex", "bonjour", "sess-1")
    # Lot 5d : sudo vers le wrapper root, jamais vers `claude` directement.
    assert cmd[:6] == ["sudo", "-n", "-H", "-u", "alex", "/usr/local/sbin/homeos-claude"]
    assert cmd[6:9] == ["-p", "bonjour", "--output-format"]
    assert "stream-json" in cmd and "--verbose" in cmd
    assert cmd[cmd.index("--resume") + 1] == "sess-1"
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == str(cfg.data_dir / ".claude-system-prompt-alex.md")
    assert cmd[cmd.index("--mcp-config") + 1] == str(cfg.root_dir / "tools" / "mcp.json")
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__homeos__*,Read"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--permission-prompts") + 1] == "none"
    # --add-dir : seulement les sous-dossiers non sensibles existants + content (capabilities absent ici),
    # jamais data/ entier (secrets, bases, état).
    add_dirs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"]
    assert add_dirs == [str(cfg.data_dir / "foyer"), str(cfg.data_dir / "personnes"), str(cfg.data_dir / "fichiers"),
                        str(cfg.root_dir / "content")]
    assert str(cfg.data_dir) not in add_dirs
    reglages = json.loads(cmd[cmd.index("--settings") + 1])
    assert reglages["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    refus = reglages["permissions"]["deny"]
    assert "Read(//etc/homeos/**)" in refus
    assert f"Read(/{cfg.data_dir}/**/secrets*)" in refus
    assert f"Read(/{cfg.data_dir}/personnes/camille/prive/**)" in refus
    assert f"Read(/{cfg.data_dir}/personnes/alex/prive/**)" not in refus
    assert "--include-partial-messages" in cmd
    assert "--dangerously-skip-permissions" not in cmd and "bypassPermissions" not in cmd

    cmd2 = construire_commande(cfg, "alex", "bonjour", None)
    assert "--resume" not in cmd2

    monkeypatch.setenv("HOMEOS_NO_SUDO", "1")
    cmd3 = construire_commande(cfg, "alex", "bonjour", None)
    assert cmd3[0] == "claude" and "sudo" not in cmd3 and "/usr/local/sbin/homeos-claude" not in cmd3

    monkeypatch.delenv("HOMEOS_NO_SUDO")
    cmd4 = construire_commande(cfg, "camille", "bonjour", None, cle_api=True)
    assert "sudo" not in cmd4 and cmd4[1] == "--bare" and "--settings" in cmd4


def test_dossier_de_travail_vide(cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cwd = dossier vide par personne, hors de data/ quand l'état est séparé (HOMEOS_ETAT)."""
    monkeypatch.delenv("HOMEOS_ETAT", raising=False)
    assert cfg.dossier_travail("alex") == cfg.data_dir / ".cwd" / "alex"
    assert cfg.sessions_path == cfg.data_dir / "sessions.json"
    monkeypatch.setenv("HOMEOS_ETAT", str(tmp_path / "etat"))
    assert cfg.dossier_travail("alex") == tmp_path / "etat-cwd" / "alex"  # lot 7 : hors de l'état (700)
    assert cfg.sessions_path == tmp_path / "etat" / "sessions.json"
    assert cfg.kd_captures_dir == tmp_path / "etat" / "kd" / "captures"
    run = ClaudeRunner(cfg).run("alex", "Alex", "x", None)
    assert run.cwd == tmp_path / "etat-cwd" / "alex"


def test_environnement_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    proxy = tmp_path / "homeos-proxy"
    proxy.write_text("HTTPS_PROXY=http://127.0.0.1:3128\nNO_PROXY=127.0.0.1,localhost\n", encoding="utf-8")
    monkeypatch.setenv("HOMEOS_FICHIER_PROXY", str(proxy))
    base = {"ANTHROPIC_API_KEY": "sk-x", "PATH": "/usr/bin"}
    env = environnement_claude(base)
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128" and env["NO_PROXY"].startswith("127.0.0.1")
    assert environnement_claude(base, cle_api=True)["ANTHROPIC_API_KEY"] == "sk-x"
    monkeypatch.setenv("HOMEOS_FICHIER_PROXY", str(tmp_path / "absent"))
    assert "HTTPS_PROXY" not in environnement_claude({"PATH": "/usr/bin"})


def test_mesures_resultat() -> None:
    raw = {
        "type": "result",
        "total_cost_usd": 0.0123,
        "duration_ms": 4567,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": 55},
    }
    assert mesures_resultat(raw) == {"cout_usd": 0.0123, "tokens_entree": 1010, "tokens_sortie": 55, "duree_ms": 4567}
    assert mesures_resultat({"usage": "?"}) == {
        "cout_usd": None, "tokens_entree": None, "tokens_sortie": None, "duree_ms": None
    }


# ---------------------------------------------------------------------------
# CONFIRMATION_REQUISE
# ---------------------------------------------------------------------------
def test_extraire_confirmation_ligne() -> None:
    texte = "Je peux l'ajouter à l'agenda.\n\nCONFIRMATION_REQUISE:42:Ajouter « Dentiste » le 12/03 à 10h\n\nTu valides ?"
    propre, confs = confirmations.extraire_confirmations(texte)
    assert "CONFIRMATION_REQUISE" not in propre
    assert propre == "Je peux l'ajouter à l'agenda.\n\nTu valides ?"
    assert len(confs) == 1
    assert confs[0].action_id == 42
    assert confs[0].resume == "Ajouter « Dentiste » le 12/03 à 10h"


def test_extraire_confirmation_inline_et_code() -> None:
    texte = "Voici : `CONFIRMATION_REQUISE:7:Créer l'événement` et c'est tout. CONFIRMATION_REQUISE:7:Créer l'événement"
    propre, confs = confirmations.extraire_confirmations(texte)
    assert "CONFIRMATION_REQUISE" not in propre
    assert [c.action_id for c in confs] == [7]
    assert confs[0].resume == "Créer l'événement"


def test_sans_confirmation() -> None:
    propre, confs = confirmations.extraire_confirmations("Rien à valider ici.")
    assert propre == "Rien à valider ici." and confs == []


def test_clavier_et_callback() -> None:
    confs = [confirmations.Confirmation(12, "x")]
    kb = confirmations.clavier_confirmation(confs)
    boutons = kb.inline_keyboard[0]
    assert [b.callback_data for b in boutons] == ["confirm:12", "cancel:12"]
    assert confirmations.parser_callback("confirm:12") == ("confirm", 12)
    assert confirmations.parser_callback("cancel:3") == ("cancel", 3)
    assert confirmations.parser_callback("autre:3") is None


def test_callback_annuler_et_deja_traitee(cfg: Config) -> None:
    db = Db(cfg.db_path)
    db.init()
    aid = db.log("outil", "agenda_ajouter", "Dentiste", {"titre": "Dentiste"}, status="pending")

    msg = asyncio.run(confirmations.traiter_callback(cfg, db, f"cancel:{aid}", "Alex"))
    assert "annulée" in msg
    assert db.action(aid)["status"] == "cancelled"

    msg2 = asyncio.run(confirmations.traiter_callback(cfg, db, f"confirm:{aid}", "Alex"))
    assert "déjà traitée" in msg2
    assert "introuvable" in asyncio.run(confirmations.traiter_callback(cfg, db, "confirm:999", "Alex"))


def test_callback_pause(cfg: Config) -> None:
    db = Db(cfg.db_path)
    db.init()
    aid = db.log("outil", "agenda_ajouter", "x", None, status="pending")
    cfg.pause_path.write_text("pause")
    msg = asyncio.run(confirmations.traiter_callback(cfg, db, f"confirm:{aid}", "Alex"))
    assert "pause" in msg
    assert db.action(aid)["status"] == "pending"


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------
def test_allowlist(cfg: Config) -> None:
    assert cfg.est_autorise(111111111)
    assert cfg.est_autorise(222222222)
    assert not cfg.est_autorise(123456789)
    p = cfg.personne_par_telegram(222222222)
    assert p == Personne(222222222, "Camille", "camille", "proprietaire")
    assert cfg.est_groupe(-100123456789)
    assert not cfg.est_groupe(111111111)


def test_allowlist_handler_journalise(cfg: Config) -> None:
    """Un intrus est ignoré et journalisé dans actions_log."""
    from types import SimpleNamespace

    from bot.handlers import verifier_acces

    db = Db(cfg.db_path)
    db.init()
    ctx = SimpleNamespace(cfg=cfg, db=db)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999, username="inconnu"),
        effective_chat=SimpleNamespace(id=-100123456789),
    )
    assert verifier_acces(ctx, update) is None
    rows = db.dernieres_actions(5)
    assert rows[0]["kind"] == "acces_refuse" and rows[0]["status"] == "ignored"
    assert "999" in rows[0]["summary"]

    update_ok = SimpleNamespace(effective_user=SimpleNamespace(id=111111111, username="a"), effective_chat=SimpleNamespace(id=1))
    assert verifier_acces(ctx, update_ok).prenom == "Alex"


def test_ligne_contexte_et_mention() -> None:
    from datetime import datetime

    quand = datetime(2026, 11, 7, 18, 30).astimezone()
    assert ligne_contexte("Alex", True, quand) == "[contexte: Alex écrit dans le groupe, 07/11/2026 18:30]"
    assert ligne_contexte("Camille", False, quand) == "[contexte: Camille écrit en privé, 07/11/2026 18:30]"
    assert nettoyer_mention("@TestBot ajoute du lait", "TestBot") == "ajoute du lait"


# ---------------------------------------------------------------------------
# Markdown → HTML Telegram
# ---------------------------------------------------------------------------
def test_md_vers_html_basique() -> None:
    md = "# Titre\n\nVoici du **gras**, de l'*italique* et du `code <b>`.\n\n- un\n- deux & trois\n* quatre\n\n1. premier\n2. second"
    html = formatting.md_vers_html(md)
    assert "<b>Titre</b>" in html
    assert "<b>gras</b>" in html
    assert "<i>italique</i>" in html
    assert "<code>code &lt;b&gt;</code>" in html
    assert "• un\n• deux &amp; trois\n• quatre" in html
    assert "1. premier\n2. second" in html
    assert "**" not in html and "# " not in html


def test_md_vers_html_echappe_et_liens() -> None:
    md = "Attention <script>alert(1)</script> et a < b.\n[site](https://example.org/x?a=1&b=2) et [mauvais](javascript:alert(1))"
    html = formatting.md_vers_html(md)
    assert "&lt;script&gt;" in html
    assert "a &lt; b" in html
    assert '<a href="https://example.org/x?a=1&amp;b=2">site</a>' in html
    assert "javascript:" not in html or "<a" not in html.split("javascript:")[0][-10:]


def test_md_vers_html_bloc_code() -> None:
    md = "Exemple :\n```python\nprint('<ok>')\n```\nfin"
    html = formatting.md_vers_html(md)
    assert "<pre>print('&lt;ok&gt;')</pre>" in html
    assert "```" not in html


def test_decouper() -> None:
    texte = "\n\n".join(f"Paragraphe {i} " + "x" * 500 for i in range(20))
    morceaux = formatting.decouper(texte, limite=4000)
    assert len(morceaux) >= 3
    assert all(len(m) <= 4000 for m in morceaux)
    assert "".join(m.replace("\n", "") for m in morceaux).replace(" ", "") == texte.replace("\n", "").replace(" ", "")


def test_decouper_reequilibre_balises() -> None:
    texte = "<b>" + "a" * 50 + "</b> <i>" + "b" * 60 + "</i>"
    morceaux = formatting.decouper(texte, limite=40)
    assert all(len(m) <= 60 for m in morceaux)
    for m in morceaux:
        assert m.count("<b>") == m.count("</b>")
        assert m.count("<i>") == m.count("</i>")


# ---------------------------------------------------------------------------
# Substitution {{assistant.nom}}
# ---------------------------------------------------------------------------
def test_substituer_nom(cfg: Config) -> None:
    assert substituer_nom("Tu es {{assistant.nom}}. {{assistant.nom}} aide.", "Testbot") == "Tu es Testbot. Testbot aide."
    cfg.system_prompt_source.write_text("# {{assistant.nom}}\nJe suis {{assistant.nom}}.", encoding="utf-8")
    cible = preparer_prompt_systeme(cfg)
    assert cible == cfg.data_dir / ".claude-system-prompt.md"
    assert cible.read_text(encoding="utf-8") == "# Testbot\nJe suis Testbot."
    # Le fichier source n'est pas modifié.
    assert "{{assistant.nom}}" in cfg.system_prompt_source.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sessions, config, db
# ---------------------------------------------------------------------------
def test_sessions_expiration(tmp_path: Path) -> None:
    from datetime import datetime, timedelta

    s = Sessions(tmp_path / "sessions.json")
    assert s.get(1) is None
    s.set(1, "alex", "sess-a")
    assert s.get(1) == "sess-a"
    data = json.loads((tmp_path / "sessions.json").read_text())
    assert data["1"]["linux_user"] == "alex" and data["1"]["session_id"] == "sess-a"
    plus_tard = datetime.now().astimezone() + timedelta(hours=13)
    assert s.get(1, now=plus_tard) is None
    s.reset(1)
    assert s.get(1) is None


def test_parse_secrets_env() -> None:
    env = parse_secrets_env('# commentaire\nTELEGRAM_BOT_TOKEN="123:abc"\nexport ANTHROPIC_API_KEY=sk-test\nVIDE=\n')
    assert env == {"TELEGRAM_BOT_TOKEN": "123:abc", "ANTHROPIC_API_KEY": "sk-test", "VIDE": ""}


def test_db_actions_log_et_courses(cfg: Config) -> None:
    db = Db(cfg.db_path)
    db.init()
    db.init()  # idempotent
    aid = db.log("Alex", "message_entrant", "x" * 600, {"a": 1})
    row = db.action(aid)
    assert len(row["summary"]) == 500 and json.loads(row["payload"]) == {"a": 1}
    assert db.courses() is None
    with db.connect() as conn:
        conn.execute("CREATE TABLE courses (id INTEGER PRIMARY KEY, article TEXT, rayon TEXT, coche INTEGER, ajoute_par TEXT, ts TEXT)")
        conn.execute("INSERT INTO courses (article, rayon, coche) VALUES ('lait', 'laitier', 0), ('pain', 'boulangerie', 1)")
    rows = db.courses()
    assert [r["article"] for r in rows] == ["lait"]
    from bot.commands import formater_courses, formater_journal

    assert "lait" in formater_courses(rows) and "pain" not in formater_courses(rows)
    assert "message_entrant" in formater_journal(db.dernieres_actions(20))


def test_construire_commande_cle_api(cfg: Config) -> None:
    from bot.claude_runner import construire_commande

    cmd = construire_commande(cfg, "camille", "bonjour", None, cle_api=True)
    assert "--bare" in cmd
    assert "sudo" not in cmd
    assert "-p" in cmd and "bonjour" in cmd


# ---------------------------------------------------------------------------
# Validation automatique par capacité (config.yaml → capacites.<nom>.validation)
# ---------------------------------------------------------------------------
def test_capacites_automatiques(cfg: Config) -> None:
    cfg.raw["capacites"] = {"kitchen_daily": {"validation": "automatique"}, "bus": {"validation": "humaine"}}
    assert cfg.capacites_automatiques() == {"kitchen_daily"}
    assert cfg.action_automatique("kitchen_daily_selection")
    assert not cfg.action_automatique("bus_reserver")
    assert not cfg.action_automatique("agenda_ajouter")
    cfg.raw["capacites"] = {}
    assert not cfg.action_automatique("kitchen_daily_selection")


def test_confirmation_toujours_malgre_validation_automatique(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capacité en validation automatique : les kinds de `confirmation_toujours` gardent le bouton."""
    cap = cfg.root_dir / "capabilities" / "kitchen_daily"
    cap.mkdir(parents=True)
    (cap / "capability.yaml").write_text(
        "nom: kitchen_daily\nactions_avec_effet: [kitchen_daily_proposer, kitchen_daily_extras_definir]\n"
        "confirmation_toujours: [kitchen_daily_extras_definir, kitchen_daily_livraison_definir]\n",
        encoding="utf-8",
    )
    (cfg.root_dir / "capabilities" / "casse").mkdir()
    (cfg.root_dir / "capabilities" / "casse" / "capability.yaml").write_text("{: pas du yaml", encoding="utf-8")
    cfg.raw["capacites"] = {"kitchen_daily": {"validation": "automatique"}}
    assert cfg.actions_confirmation_toujours() == {"kitchen_daily_extras_definir", "kitchen_daily_livraison_definir"}

    db = Db(cfg.db_path)
    db.init()
    plats = db.log("outil", "kitchen_daily_selection", "6 plats", {}, status="pending")
    extras = db.log("outil", "kitchen_daily_extras_definir", "Extras", {}, status="pending")
    semaine = db.log("outil", "kitchen_daily_livraison_definir", "Semaine sautée", {}, status="pending")
    executees: list[str] = []

    async def faux_callback(cfg_: Config, db_: Db, data: str, who: str, envoyer_photo: object = None) -> str:
        executees.append(data)
        return f"✅ Validé par {who}.\nFait"

    monkeypatch.setattr(confirmations, "traiter_callback", faux_callback)
    confs = [confirmations.Confirmation(a, "x") for a in (plats, extras, semaine)]
    restantes, resultats = asyncio.run(confirmations.executer_automatiquement(cfg, db, confs, "Alex"))
    assert executees == [f"confirm:{plats}"]
    assert [c.action_id for c in restantes] == [extras, semaine]
    assert resultats == ["✅ Fait automatiquement.\nFait"]

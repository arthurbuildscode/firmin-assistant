"""Commande Telegram /identifiants (lot 5b) — sans Telegram ni réseau."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram.error import BadRequest

from bot import commands
from bot.config import Config, parse_config
from bot.db import Db

SECRET = "Hunter2-Mot-De-Passe!"
CONFIG_RAW = {
    "assistant": {"nom": "Firmin"},
    "personnes": [{"telegram_id": 111111111, "prenom": "Alex", "linux_user": "alex", "role": "proprietaire"}],
}


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir()
    return parse_config(CONFIG_RAW, data, tmp_path / "repo")


class FauxMessage:
    def __init__(self, peut_effacer: bool = True) -> None:
        self.peut_effacer = peut_effacer
        self.efface = False
        self.reponses: list[str] = []

    async def delete(self) -> bool:
        if not self.peut_effacer:
            raise BadRequest("Message can't be deleted")
        self.efface = True
        return True

    async def reply_text(self, texte: str, **_kw: object) -> None:
        self.reponses.append(texte)


class FauxBot:
    def __init__(self) -> None:
        self.envois: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, texte: str, **_kw: object) -> None:
        self.envois.append((chat_id, texte))


def _appeler(cfg: Config, args: list[str], peut_effacer: bool = True) -> tuple[FauxMessage, FauxBot, Db]:
    db = Db(cfg.db_path)
    db.init()
    ctx = SimpleNamespace(cfg=cfg, db=db)
    msg = FauxMessage(peut_effacer)
    bot = FauxBot()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=111111111, username="alex"),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_message=msg,
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"ctx": ctx}), args=args, bot=bot)
    asyncio.run(commands.cmd_identifiants(update, context))
    return msg, bot, db


def test_analyser_identifiants() -> None:
    assert commands.analyser_identifiants([]) == ("aide", None)
    assert commands.analyser_identifiants(None) == ("aide", None)
    assert commands.analyser_identifiants(["Planity"]) == ("capacite", "planity")
    assert commands.analyser_identifiants(["cantine-école"]) == ("capacite", "cantine_ecole")
    assert commands.analyser_identifiants(["planity", SECRET]) == ("refus", None)
    assert commands.analyser_identifiants([SECRET]) == ("refus", None)


def test_identifiants_capacite_donne_la_commande(cfg: Config) -> None:
    msg, bot, db = _appeler(cfg, ["planity"])
    assert len(msg.reponses) == 1 and not bot.envois
    texte = msg.reponses[0]
    assert "homeos-coffre set planity/alex" in texte and "sudo -u homeos" in texte
    assert "quels identifiants sont enregistrés" in texte
    assert "jamais ici" in texte
    assert db.dernieres_actions(1)[0]["summary"] == "/identifiants planity"


def test_identifiants_sans_argument(cfg: Config) -> None:
    msg, _bot, _db = _appeler(cfg, [])
    assert "Usage : /identifiants" in msg.reponses[0]


def test_identifiants_valeur_collee_effacee_et_jamais_journalisee(cfg: Config) -> None:
    msg, bot, db = _appeler(cfg, ["planity", SECRET])
    assert msg.efface and not msg.reponses
    assert bot.envois and bot.envois[0][0] == 42
    texte = bot.envois[0][1]
    assert "n'enregistre jamais" in texte and "change ce mot de passe" in texte and SECRET not in texte
    for row in db.dernieres_actions(20):
        assert SECRET not in " ".join(str(v) for v in tuple(row))


def test_identifiants_valeur_collee_non_effacable(cfg: Config) -> None:
    msg, bot, _db = _appeler(cfg, [SECRET], peut_effacer=False)
    assert not msg.efface and not bot.envois
    assert "supprime-le toi-même" in msg.reponses[0] and SECRET not in msg.reponses[0]


def test_identifiants_dans_l_aide() -> None:
    assert "identifiants" in [c for c, _d in commands.COMMANDES]
